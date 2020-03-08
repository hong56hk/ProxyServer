'''
version: 20200302

'''
import threading
import selectors
import datetime
import argparse
import logging
import socket
import queue
import time

import Constants
import util
   
###############################################################################        
class ProxyClient():
  def __init__(self, proxy, name="ProxyClient", loglvl=logging.WARNING, logpath=None):
    if logpath:
      logging.basicConfig(filename=logpath, filemode="a", format='%(asctime)s.%(msecs)03d <%(levelname).4s> %(threadName)s: %(message)s',datefmt='%Y-%m-%d %H:%M:%S', level=loglvl)
    else:
      logging.basicConfig(format='%(asctime)s.%(msecs)03d <%(levelname).4s> %(threadName)s: %(message)s',datefmt='%Y-%m-%d,%H:%M:%S', level=loglvl)
    self.logger = logging.getLogger("logger")

    (self.proxy_ip, self.proxy_port) = util.decodeHostPort(proxy)
    self.proxy_last_com_time = None # proxy last communication time
    self.running = True
    self.name = name
    self.msg_seq = 0
    
    self.proxy_socket = None
    self.proxy_socket_lock = threading.Lock()
    self.remote_socket_dict = {}  # key: byte(home.host / msg.from_host), obj: target_socket
    self.remote_socket_dict_lock = threading.Lock()

    self.socket_to_host_dict = {} # key: target_socket, obj: byte(home.host / msg.from_host)
    self.socket_to_host_dict_lock = threading.Lock()

    self.remote_selector = selectors.DefaultSelector()
    self.remote_send_buffer = queue.Queue()
    self.proxy_send_buffer = queue.Queue()

    self.proxy_connected = threading.Event()
    self.proxy_connected.clear()
    # self.proxy_disconnected = threading.Event()
    # self.proxy_disconnected.set()

  ##-------------
  # client
  # hex format of from_host and to_host
  def sendDisconnectRemote(self, from_host, to_host, reason=''):
    self.msg_seq += 1
    msg = util.encodeMessage(self.msg_seq, Constants.CMD_REMOTE_DISCONNECT, from_host, to_host, reason.encode())  # tell server the remote cannot be connected
    self.proxy_send_buffer.put(msg)

  # hex format of from_host and to_host
  def disconnectRemote(self, socket, from_host='', to_host=''):
    if socket:
      self.logger.info("disconnecting remote... {}->{}".format(util.fromHexHost(from_host), util.fromHexHost(to_host)))
      
      self.remote_selector.unregister(socket)
      if socket and not socket._closed:
        try:
          # socket.shutdown(socket.SHUT_RDWR)
          socket.close()
          self.logger.info("disconnected remote")
        except Exception as e:
          self.logger.error("error when disconnecting remote: {}".format(e))
      else:
        self.logger.info("remote does not connect")

      self.socket_to_host_dict_lock.acquire()
      if socket in self.socket_to_host_dict:
        del self.socket_to_host_dict[socket]
      self.socket_to_host_dict_lock.release()

      self.remote_socket_dict_lock.acquire()
      for hostkey in self.remote_socket_dict.keys():
        ss = self.remote_socket_dict[hostkey]
        if ss == socket:
          del self.remote_socket_dict[hostkey]
          break
      self.remote_socket_dict_lock.release()
      
    else:
      self.logger.info("remote socket is null")

  ##------------
  def disconnectProxy(self):
    self.proxy_socket_lock.acquire()
    self.logger.info("disconnecting proxy...")
    if self.proxy_connected.is_set():
      self.proxy_connected.clear()
      self.proxy_send_buffer.put(None)
      self.proxy_socket.close()
      self.proxy_socket = None
      self.logger.info("disconnected proxy")
    else:
      self.logger.info("proxy does not connect")
    self.proxy_socket_lock.release()

  ##-------------
  def handlingProxyMsg(self, msg):
    (seq, cmd, from_host, to_host, payload) = util.decodeMessage(msg)
    self.logger.debug("read data from proxy: cmd: {}, {}->{}, seq: {}, data: {}".format(cmd, util.fromHexHost(from_host), util.fromHexHost(to_host), seq, payload[:Constants.LOG_MSG_SIZE]))

    #--------------
    if cmd == Constants.CMD_GREETING:
      self.logger.info("greeting from proxy: {}".format(payload))

    #--------------
    elif cmd == Constants.CMD_CONTROL:
      payload = ''
      for key, val in self.remote_socket_dict.items():
        payload += "{}:{}\n".format(key, "close" if val._closed else "open")
      self.proxy_send_buffer.put(payload.encode())

    #--------------
    elif cmd == Constants.CMD_FORWARD:
      self.remote_send_buffer.put(msg)

    #--------------
    elif cmd == Constants.CMD_REMOTE_DISCONNECT:
      # 3: CMD, 6:disconnect remote, 6:to be told remote, other: reason
      remote_ip, remote_port = util.fromHexHost(from_host)
      to_ip, to_port = util.fromHexHost(to_host)

      if from_host not in self.remote_socket_dict:
        self.logger.error("error: cannot disconnect, no remote {}:{} found: {}".format(remote_ip, remote_port, payload))
      else:
        self.logger.info("the other side's remote {}:{}->{}:{} disconnected: {}".format(remote_ip, remote_port, to_ip, to_port, payload))
        remote_socket = self.remote_socket_dict[from_host]
        self.disconnectRemote(remote_socket, from_host, to_host)
      
    #--------------
    elif cmd == Constants.CMD_ALV:
      # this is the keepalive echo from server, calc the latency
      round_trip = time.time() - (util.intFromHex(payload[0:4]) + util.shortFromHex(payload[4:6]) / 1000.0)
      self.logger.info("recved keepalive echo, ping: {}ms".format(int(round_trip * 1000.0)))

    else:
      self.logger.error("unknown cmd from proxy: {}".format(cmd))

  ##-------------
  ## client
  def proxyRecvThread(self):
    self.logger.info("started")
    tmp_recv_buf = b''
    supposed_len = 0

    while self.running:
      if len(tmp_recv_buf) < max(Constants.MIN_MSG_LEN, supposed_len + 2):
        # not enough to determine the message
        try:
          self.proxy_connected.wait()
          new_recv = self.proxy_socket.recv(Constants.BUFFER_SIZE)
          if not new_recv:
            self.logger.info("recv null from proxy..")
            self.disconnectProxy()
          else:
            tmp_recv_buf += new_recv
            self.proxy_last_com_time = time.time()
        except Exception as e:
          self.logger.error("error when recv from proxy: {}".format(e))
          self.disconnectProxy()
      else:
        while len(tmp_recv_buf) >= Constants.MIN_MSG_LEN:
          supposed_len = util.shortFromHex(tmp_recv_buf[0:2])
          if len(tmp_recv_buf) < supposed_len + 2: 
            break # fragment
          
          msg = tmp_recv_buf[2:supposed_len + 2]
          if len(tmp_recv_buf) > supposed_len + 2:
            # keep the remaining
            tmp_recv_buf = tmp_recv_buf[(supposed_len + 2):]
          else:
            tmp_recv_buf = b''
            supposed_len = 0
          
          # handle the message from Proxy server
          self.handlingProxyMsg(msg)

      # if self.proxy_server_socket is None:
    self.logger.info("ended")

  ##-------------
  ## client
  def proxySendThread(self):
    self.logger.info("started")
    send_buffer = None
    while self.running:
      # check connection, connect first
      if not self.proxy_connected.is_set():
        try:
          self.proxy_socket_lock.acquire()
          self.logger.info("connecting to proxy...{}:{}".format(self.proxy_ip, self.proxy_port))
          self.proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
          #self.proxy_socket.settimeout(Constants.CONNECTION_TIMEOUT)
          self.proxy_socket.connect((self.proxy_ip, self.proxy_port))
          self.logger.info("connected to proxy")
        except Exception as e:
          self.logger.error("error when connecting to proxy...{}".format(e))
          self.proxy_socket = None
        else:
          self.proxy_last_com_time = time.time()
          self.proxy_connected.set()
        finally:
          self.proxy_socket_lock.release()

        time.sleep(Constants.RECONNECT_INTERVAL)

      else: # connected
        if send_buffer is None:
          send_buffer = self.proxy_send_buffer.get()
        if send_buffer:
          try:
            self.proxy_connected.wait()
            self.logger.debug("sending to proxy: {}: {}".format(util.shortFromHex(send_buffer[2:4]), send_buffer[:Constants.LOG_MSG_SIZE]))
            self.proxy_socket.sendall(send_buffer)
          except Exception as e:
            self.logger.error("error when sending data to proxy: {}".format(e))
            self.disconnectProxy()
          else:
            self.proxy_last_com_time = time.time()
            send_buffer = None
        # else being noticed proxy disconnected  
    self.logger.info("ended")

  ##-------------
  ## client
  # the msg is from proxy server to remote
  def recvFromRemote(self, sock, msg):
    (seq, cmd, from_host, to_host, payload) = util.decodeMessage(msg)
    try:
       # remote_host = sock.getpeername()
      data = sock.recv(Constants.BUFFER_SIZE)

      if data:
        self.logger.debug("recv from remote {}: {}".format(sock.getpeername(), data[:Constants.LOG_MSG_SIZE]))
        self.msg_seq += 1
        msg = util.encodeMessage(self.msg_seq, Constants.CMD_FORWARD, to_host, from_host, data)
        self.proxy_send_buffer.put(msg)
      else:
        self.logger.info("recv null from remote {}".format(sock.getsockname()))
        self.disconnectRemote(sock, from_host, to_host)
        self.sendDisconnectRemote(to_host, from_host)

    except Exception as e:
      self.logger.error("error when recv from remote {}: {}".format(sock.getsockname(), e))
      self.disconnectRemote(sock, from_host, to_host)
      self.sendDisconnectRemote(to_host, from_host)
      
  ##-------------
  def connectRemote(self, sock, msg):
    (seq, cmd, from_host, to_host, payload) = util.decodeMessage(msg)
    (to_ip, to_port) = util.fromHexHost(to_host)

    conn_result = sock.connect_ex((to_ip, to_port))
    if conn_result == 0:
      self.logger.info("connected to {}".format(sock.getpeername()))
      self.remote_selector.modify(sock, selectors.EVENT_READ, msg)

      self.remote_socket_dict_lock.acquire()
      self.remote_socket_dict[from_host] = sock
      self.remote_socket_dict_lock.release()

      self.socket_to_host_dict_lock.acquire()
      self.socket_to_host_dict[sock] = from_host
      self.socket_to_host_dict_lock.release()

      # put back to send buffer
      self.remote_send_buffer.put(msg)
    else:
      # send back error #socket.errno.errorcode[status]
      self.remote_selector.unregister(sock)
      fail_msg = socket.errno.errorcode[conn_result]
      self.logger.info("connection failed: {}:{}".format(conn_result, fail_msg))
      self.sendDisconnectRemote(to_host, from_host, fail_msg)

  ##-------------
  def remoteRecvThread(self):
    self.logger.info("started")
    while self.running:
      events = self.remote_selector.select()
      for key, mask in events:
        msg = key.data
        sock = key.fileobj

        if mask == selectors.EVENT_WRITE:
          self.connectRemote(sock, msg)
        
        elif mask == selectors.EVENT_READ:
          self.recvFromRemote(sock, msg)
    self.logger.info("ended")

  ##-------------
  # client
  def remoteSendThread(self):
    self.logger.info("started")
    while self.running:
      msg = self.remote_send_buffer.get()
      (seq, cmd, from_host, to_host, payload) = util.decodeMessage(msg)
      (from_ip, from_port) = util.fromHexHost(from_host)
      (to_ip, to_port) = util.fromHexHost(to_host)

      try:
        if from_host not in self.remote_socket_dict:
          # if not connected -> connect now
          (remote_ip, remote_port) = util.fromHexHost(to_host)
          remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
          remote_socket.settimeout(5)
          remote_socket.setblocking(0)
          remote_socket.connect_ex((remote_ip, remote_port)) # expect to be in progress
          self.remote_selector.register(remote_socket, selectors.EVENT_WRITE, msg)
       
        else:
          self.remote_socket_dict_lock.acquire()
          remote_socket = self.remote_socket_dict[from_host]
          self.remote_socket_dict_lock.release()

          try:
            self.logger.debug("sending to remote {}: {}".format(remote_socket.getpeername(), payload[:Constants.LOG_MSG_SIZE]))
            remote_socket.sendall(payload)

          except Exception as e:
            self.logger.error("error when sending data to remote: {}".format(e))
            self.disconnectRemote(remote_socket, to_host, from_host)
            self.sendDisconnectRemote(to_host, from_host, str(e))

      except Exception as e:
        self.logger.critical("error when connecting to {}:{}".format(remote_ip, remote_port))
        self.disconnectRemote(remote_socket, to_host, from_host)
        self.sendDisconnectRemote(to_host, from_host, str(e))
        
    self.logger.info("ended")

  def proxyKeepAliveThread(self):
    self.logger.info("started")
    while self.running:
      if self.proxy_connected.is_set():
        curr_time = time.time()
        since_last_msg = curr_time - self.proxy_last_com_time
        if since_last_msg > Constants.HEART_BEAT_INTERVAL:
          payload = util.intToHex(int(curr_time)) + util.shortToHex(int((curr_time*1000)%1000))
          # send a heart beat
          self.msg_seq += 1
          msg = util.encodeMessage(self.msg_seq, Constants.CMD_ALV, util.toHexHost(self.proxy_ip, self.proxy_port), util.toHexHost(self.proxy_ip, self.proxy_port), payload)
          self.proxy_send_buffer.put(msg)
          self.logger.info("added keep alive to proxy send buffer")
      time.sleep(Constants.HEART_BEAT_INTERVAL)
    self.logger.info("ended")

  ##-------------
  def start(self):
    self.logger.info("started")
    proxyKeepAliveThread = threading.Thread(target=self.proxyKeepAliveThread, name="proxyKeepAliveThread")
    proxyRecvThread = threading.Thread(target=self.proxyRecvThread, name="proxyRecvThread")
    remoteRecvThread = threading.Thread(target=self.remoteRecvThread, name="remoteRecvThread")
    proxySendThread = threading.Thread(target=self.proxySendThread, name="proxySendThread")
    remoteSendThread = threading.Thread(target=self.remoteSendThread, name="remoteSendThread")

    proxyKeepAliveThread.start()
    proxyRecvThread.start()
    remoteRecvThread.start()
    proxySendThread.start()
    remoteSendThread.start()

    proxyKeepAliveThread.join()
    proxyRecvThread.join()
    remoteRecvThread.join()
    proxySendThread.join()
    remoteSendThread.join()

    self.logger.info("ended")


###############################################################################
# main
def main(args):
  if not args.proxy:
    print("please enter the proxy server ip and port")
    exit()

  loglvl = int(args.verbose) if args.verbose else None
  logpath = args.log if args.log else None
  client = ProxyClient(args.proxy, loglvl=loglvl, logpath=logpath)
  client.start()
  

if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-p", "--proxy", required=True, help="Proxy server blinding address and port <ip:port>")
  ava_log_lvl = [logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
  parser.add_argument("-v", "--verbose", type=int, default=logging.INFO, help=", ".join(["{}:{}".format(lvl, logging.getLevelName(lvl)) for lvl in ava_log_lvl]))
  parser.add_argument("-l", "--log", help="path of the log file")

  args = parser.parse_args()

  main(args)