'''
version: 20200304

'''
import threading
import selectors
import datetime
import argparse
import logging
import signal
import socket
import queue
import time
import json
import sys
import os
import DBConnector
import Constants
import Tunnel
import util



###############################################################################
class ProxyServer():
  def __init__(self, proxy, control, dbpath, routingfile="routingtable.conf", configfile="config.json", name="ProxyServer", loglvl=logging.ERROR, logpath=None):
    if logpath:
      logging.basicConfig(filename=logpath, filemode="a", format='%(asctime)s.%(msecs)03d <%(levelname).4s> %(threadName)s: %(message)s',datefmt='%Y-%m-%d %H:%M:%S', level=loglvl)
    else:
      logging.basicConfig(format='%(asctime)s.%(msecs)03d <%(levelname).4s> %(threadName)s: %(message)s',datefmt='%Y-%m-%d,%H:%M:%S', level=loglvl)
    self.logger = logging.getLogger("logger")
        
    (self.proxy_ip, self.proxy_port) = util.decodeHostPort(proxy)
    (self.control_ip, self.control_port) = util.decodeHostPort(control)
    self.routingfile = routingfile
    self.routing = {}  # key: listening port; obj: (target ip, target port)

    self.dbconn = DBConnector.DBConnector(dbpath)
    self.configfile = configfile
    self.config = { } #key: username, obj: str(ip)

    self.name = name
    self.running = True

    self.msg_seq = 0
    self.proxy_socket = None
    self.proxy_server_socket = None
    self.remote_server_socket_dict = {}
    self.remote_socket_array = []
    self.control_socket = None

    self.tunnel_dict_lock = threading.Lock()
    self.tunnel_dict = {} # key: bytes(controller_host), obj: Tunnel

    self.remote_selector = {}
    self.control_selector = None
    self.remote_send_buffer = queue.Queue()
    self.proxy_send_buffer = queue.Queue()

    self.proxy_connected = threading.Event()
    self.proxy_connected.clear()


  ##------------
  def readConfig(self, path):
    try:
      configfile = open(path, "r")
      config = json.load(configfile)
      configfile.close()

      self.logger.info("read config:")
      self.logger.info("{}".format(config))

      return config

    except Exception as e:
      self.logger.critical("error: {}".format(e))
      return None

  # write self.config into file
  # def updateConfigFile(self, path):
  #   try:
  #     configfile = open(path, "w")
  #     json.dump(self.config, configfile, indent=4, sort_keys=True)
  #     configfile.close()

  #   except Exception as e:
  #     self.logger.critical("error: {}".format(e))
      
  ##------------
  def readRoutingTable(self, path):
    config = []
    self.logger.info("reading config...")
    infile = open(path, "r")
    for line in infile:
      line = line.strip()
      if line and len(line) > 1 and line[0] != "#":
        values = line.split(" ")
        if len(values) < 2:
          self.logger.info("invalid line: {}".format(line))
          break
        (listen_ip, listen_port) = util.decodeHostPort(values[0])
        (target_ip, target_port) = util.decodeHostPort(values[1])
        config.append({
          "listen": (listen_ip, listen_port),
          "target": (target_ip, target_port)
        })
      else:
        # comment line
        pass
    infile.close()

    for i in range(len(config)):
      self.logger.info("{} listening {} -> {}".format(i, config[i]['listen'], config[i]['target']))
    self.logger.info("done")

    return config

  ##------------
  def shutdown(self, sig, frame):
    self.logger.info("shutting down server...")
    self.running = False
    
    self.logger.info("closing control socket...")
    self.control_socket.shutdown(socket.SHUT_RDWR)
    self.control_socket.close()

    self.logger.info("closing remote socket...")
    while self.remote_socket_array:
      sock = self.remote_socket_array.pop()
      sock.shutdown(socket.SHUT_RDWR)
      sock.close()

    self.logger.info("closing remote server socket...")
    for hostkey in self.remote_selector.keys():
      self.remote_server_socket_dict[hostkey].shutdown(socket.SHUT_RDWR)
      self.remote_server_socket_dict[hostkey].close()

    self.logger.info("closing proxy server socket...")
    self.proxy_server_socket.shutdown(socket.SHUT_RDWR)
    self.proxy_server_socket.close()

    self.disconnectProxy()

    # unlock all threads
    self.proxy_send_buffer.put(None)
    self.remote_send_buffer.put(None)
    sys.exit(0)

  ##------------
  def tunnelWithSocket(self, sock):
    result = None
    self.tunnel_dict_lock.acquire()
    for tunnel in self.tunnel_dict.values():
      if tunnel.socket == sock:
        result = tunnel
        break
    self.tunnel_dict_lock.release()
    return result

  ##------------
  def disconnectProxy(self):
    self.logger.info("disconnecting proxy...")
    if self.proxy_connected.is_set():
      self.proxy_connected.wait()
      if self.proxy_connected.is_set():
        self.proxy_send_buffer.put(None)
        self.proxy_socket.shutdown(socket.SHUT_RDWR)
        self.proxy_socket.close()
        self.proxy_socket = None
        self.logger.info("disconnected proxy")
        self.proxy_connected.clear()
    else:
      self.logger.info("proxy does not connect")
  
  ##------------
  #controller_host hex format
  def disconnectRemote(self, controller_host):
    self.logger.info("disconnecting remote... {}".format(util.fromHexHost(controller_host)))
    
    self.tunnel_dict_lock.acquire()
    if controller_host in self.tunnel_dict:
      tunnel = self.tunnel_dict[controller_host]
      
      # unregister selector
      if tunnel.target_host in self.remote_selector:
        try:
          self.remote_selector[tunnel.target_host].unregister(tunnel.socket)
        except Exception as e:
          self.logger.critical("error when unregister socket: {}".format(e))
      else:
        self.logger.info("error when unregister socket: {}".format(util.fromHexHost(controller_host)))

      # close socket
      if tunnel.socket and not tunnel.socket._closed:
        try:    
          tunnel.socket.close()
          tunnel.socket = None
          self.logger.info("disconnected remote")
        except Exception as e:
          self.logger.error("error when disconnecting remote: {}".format(e))
      else:
        self.logger.info("remote does not connect")
      del self.tunnel_dict[controller_host]
    else:
      self.logger.info("unknown remote: {}".format(util.fromHexHost(controller_host)))
    self.tunnel_dict_lock.release()
    
  ##------------
  # from_host = hex host
  # to_host = hex format
  def sendDisconnectRemote(self, from_host, to_host, reason=None):
    self.msg_seq += 1
    msg = util.encodeMessage(self.msg_seq, Constants.CMD_REMOTE_DISCONNECT, from_host, to_host, reason.encode() if reason else None)  # tell server the remote cannot be connected
    self.proxy_send_buffer.put(msg)
  
  ##------------
  def handleProxyMsg(self, msg):
    (seq, cmd, from_host, to_host, payload) = util.decodeMessage(msg)
    self.logger.debug("read data from proxy: cmd: {}, {}->{}: seq: {}, data: {}".format(cmd, util.fromHexHost(from_host), util.fromHexHost(to_host), seq, payload[:Constants.LOG_MSG_SIZE]))

    if cmd == Constants.CMD_GREETING:
      self.logger.info("greeting from proxy: {}".format(payload))
    
    elif cmd == Constants.CMD_FORWARD:
      self.remote_send_buffer.put(msg)

    elif cmd == Constants.CMD_REMOTE_DISCONNECT:
      # 3: CMD, 6:disconnect remote, 6:to be told remote, other: reason
      from_ip, from_port = util.fromHexHost(from_host)
      to_ip, to_port = util.fromHexHost(to_host)

      if to_host not in self.tunnel_dict:
        self.logger.error("error: no remote {}:{} found: {}".format(to_ip, to_port, payload))
      else:
        self.logger.info("the other side's remote {}:{}->{}:{} disconnected: {}".format(from_ip, from_port, to_ip, to_port, payload))
        self.disconnectRemote(to_host)
    
    elif cmd == Constants.CMD_ALV:
      # echo back
      latency = time.time() - (util.intFromHex(payload[0:4]) + util.shortFromHex(payload[4:6]) / 1000.0)
      self.logger.info("recved proxy client keepalive, latency: {}ms".format(int(latency * 1000.0)))
      self.msg_seq += 1
      out_msg = util.encodeMessage(self.msg_seq, cmd, to_host, from_host, payload)
      self.proxy_send_buffer.put(out_msg)

    else:
      self.logger.error("unknown cmd from proxy: {}".format(cmd))
      
  ##------------
  def proxyRecvThread(self):
    self.logger.info("started")
    tmp_recv_buf = b''
    supposed_len = 0
    created_sock = False
    while not created_sock:
      try:
        self.proxy_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.proxy_server_socket.bind((self.proxy_ip, self.proxy_port))
        self.proxy_server_socket.listen(1)
      except Exception as e:
        self.logger.error("error when binding proxy server socket: {}".format(e))
        self.logger.error("retry after 5s")
        time.sleep(5)
      else:
        self.logger.info("proxy server socket binded successfully")
        created_sock = True
    
    while self.running:
      if self.proxy_socket is None:
        self.logger.info("waiting proxy connection...")
        self.proxy_socket, proxy_addr = self.proxy_server_socket.accept()
        if proxy_addr[0] not in self.proxy_whitelist:
          self.logger.info("unauth connection from: {}".format(proxy_addr))
          self.proxy_socket.shutdown(socket.SHUT_RDWR)
          self.proxy_socket.close()
          self.proxy_socket = None
          continue
        self.logger.info("connected by proxy: {}".format(proxy_addr))
        self.proxy_connected.set()
      else:
        if len(tmp_recv_buf) < max(Constants.MIN_MSG_LEN, supposed_len + 2 ):
          # not enough to determine the message
          try:
            new_recv = self.proxy_socket.recv(Constants.BUFFER_SIZE)
            if not new_recv:
              self.logger.info("recv null from proxy")
              self.disconnectProxy()
            else:
              tmp_recv_buf += new_recv
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
            
            self.handleProxyMsg(msg)
      
      # if self.proxy_server_socket is None:
    self.logger.info("ended")

  #--------------------------
  # server
  def proxySendThread(self):
    self.logger.info("started")
    send_buffer = None
    while self.running:
      self.proxy_connected.wait()
      send_buffer = self.proxy_send_buffer.get()
      if send_buffer:
        try:
          self.logger.debug("sending to proxy: seq:{} :{}".format(util.shortFromHex(send_buffer[2:4]), send_buffer[:Constants.LOG_MSG_SIZE]))
          self.proxy_socket.sendall(send_buffer)
        except Exception as e:
          self.logger.info("error when sending to proxy: {}".format(e))
          self.disconnectProxy()
      # else: being noticed proxy disconnected  
    self.logger.info("ended")

  #--------------------------
  def recvFromRemote(self, remote_socket, mask, target_ip, target_port):
    try:
      tunnel = self.tunnelWithSocket(remote_socket)
      data = remote_socket.recv(Constants.BUFFER_SIZE)
      if data:
        self.logger.debug("recv from remote {}->{}: {}".format(util.fromHexHost(tunnel.controller_host), util.fromHexHost(tunnel.target_host), data[:Constants.LOG_MSG_SIZE]))
        self.msg_seq += 1
        msg = util.encodeMessage(self.msg_seq, Constants.CMD_FORWARD, tunnel.controller_host, tunnel.target_host, data)
        self.proxy_send_buffer.put(msg)
      else:
        self.logger.info("recv null from remote {}".format(util.fromHexHost(tunnel.controller_host)))
        self.disconnectRemote(tunnel.controller_host) 
        self.sendDisconnectRemote(tunnel.controller_host, tunnel.target_host)
    except Exception as e:
      try:
        (controller_ip, controller_port) = util.fromHexHost(tunnel.controller_host)
        self.logger.info("connection closed {}->{}: {}".format(util.fromHexHost(tunnel.controller_host), util.fromHexHost(tunnel.target_host), e))
        self.disconnectRemote(tunnel.controller_host)
        self.sendDisconnectRemote(tunnel.controller_host, tunnel.target_host)

      except Exception as e:
        self.logger.critical("error when closing socket: {}".format(e))
        self.logger.critical("cannot close the other side's remote socket")

  #--------------------------
  def acceptRemote(self, server_socket, mask, target_ip, target_port):
    try:
      target_host  = util.toHexHost(target_ip, target_port)
      remote_socket, (remote_ip, remote_port) = server_socket.accept()
      remote_socket.setblocking(False)
      remote_socket.settimeout(60)

      try:
        user = self.dbconn.login(remote_ip)
      except Exception as e:
        self.logger.error("error when getting user from db: {}".format(e))
      else:
        if not user:
          self.logger.info("unauth connection from: {} -> {}:{}".format(remote_ip, target_ip, target_port))
          remote_socket.shutdown(socket.SHUT_RDWR)
          remote_socket.close()
        else:
          controller_host = util.toHexHost(remote_ip, remote_port)
          tunnel = Tunnel.Tunnel(controller_host, target_host, remote_socket)
          self.tunnel_dict_lock.acquire()
          self.tunnel_dict[controller_host] = tunnel
          self.tunnel_dict_lock.release()
          self.logger.info("accepted {} {}:{}->{}:{}".format(user['username'], remote_ip, remote_port, target_ip, target_port))
          self.remote_selector[target_host].register(remote_socket, selectors.EVENT_READ, self.recvFromRemote)
    
    except OSError as e:
      if e.errno == socket.EBADF: # socket closed normally
        pass
    except Exception as e:
      self.logger.info("error when accepting new client: {}".format(e))
      

  #--------------------------
  # listen_host: 123.123.123.123:1111
  # target: 192.1.1.1:2222
  def remoteRecvThread(self, listen_ip, listen_port, target_ip, target_port):
    self.logger.info("started")
    created_sock = False
    hostkey = util.toHexHost(target_ip, target_port)
    while not created_sock:
      try:
        self.remote_server_socket_dict[hostkey] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.remote_server_socket_dict[hostkey].bind((listen_ip, listen_port))
        self.remote_server_socket_dict[hostkey].setblocking(False)
        self.remote_server_socket_dict[hostkey].settimeout(60)
        self.remote_server_socket_dict[hostkey].listen(Constants.MAX_REMOTE)
      except Exception as e:
        self.logger.error("error when binding remote server socket: {}".format(e))
        self.logger.error("retry after 5s")
        time.sleep(5)
      else:
        self.logger.info("remote server socket binded successfully")
        created_sock = True

    self.remote_selector[hostkey] = selectors.DefaultSelector()
    self.remote_selector[hostkey].register(self.remote_server_socket_dict[hostkey], selectors.EVENT_READ, self.acceptRemote)

    while self.running:
      try:
        events = self.remote_selector[hostkey].select(timeout=5)
        for key, mask in events:
          callback = key.data
          callback(key.fileobj, mask, target_ip, target_port)
      except Exception as e:
        self.logger.critical("error in selector: {}".format(e))
    
    self.remote_selector[hostkey].unregister(self.remote_server_socket_dict[hostkey])
    self.logger.info("ended")

  #--------------------------
  def remoteSendThread(self):
    self.logger.info("started")
    while self.running:
      msg = self.remote_send_buffer.get()
      if msg:
        (seq, cmd, from_host, to_host, payload) = util.decodeMessage(msg)
        
        self.tunnel_dict_lock.acquire()
        have_to_host = (to_host in self.tunnel_dict)
        tunnel = self.tunnel_dict[to_host] if have_to_host else None
        self.tunnel_dict_lock.release()

        if not have_to_host:
          self.logger.error("unknown to_host: {}".format(util.fromHexHost(to_host)))
          self.sendDisconnectRemote(to_host, from_host, "remote disconnected already")
        else:
          try:
            self.logger.debug("sending to {}: {}".format(util.fromHexHost(tunnel.controller_host), payload[:Constants.LOG_MSG_SIZE]))
            tunnel.socket.sendall(payload)
          except Exception as e:
            self.logger.info("error when sending to remote: {}".format(e))
            self.disconnectRemote(to_host)
      # else: the server has been terminated

    self.logger.info("ended")


 #--------------------------
  def acceptControl(self, server_socket, mask):
    try:
      control_socket, client_addr = server_socket.accept()
      control_socket.setblocking(False)
      self.control_selector.register(control_socket, selectors.EVENT_READ, self.recvFromControl)
    except OSError as e:
      if e.errno == socket.EBADF: # socket closed normally
        pass
    except Exception as e:
      self.logger.info("error when accepting new control client: {}".format(e))
    else:
      self.logger.info("accepted new control client: {}".format(client_addr))

  ##------------
  def recvFromControl(self, control_socket, mask):
    try:
      data = control_socket.recv(Constants.BUFFER_SIZE)
      if data:
        try:
          self.logger.info("recv msg: {}".format(data))
          msg = data.decode().strip().split(" ")
          resp = ''

          if msg[0] == "sessions":
            resp += "tunnel_dict:\n"
            session_count = 0
            self.tunnel_dict_lock.acquire()
            for (controller_host, tunnel) in self.tunnel_dict.items():
              session_count += 1
              resp += "{}->{}: {} \n".format(util.fromHexHost(tunnel.controller_host), util.fromHexHost(tunnel.target_host), "closed" if tunnel.socket._closed else "opened")
            self.tunnel_dict_lock.release()
            resp += "\n"  
            resp += "total sessions: {}\n".format(session_count)

          elif msg[0] == "set":
            if msg[1] == "loglvl":
              lvl = int(msg[2])
              
              ava_log_lvl = [logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
              

              if lvl in ava_log_lvl:
                self.logger.setLevel(lvl)
                resp = "set log level to {}".format(logging.getLevelName(lvl))
                self.logger.info("updated config")
              else:
                resp = "log level should be\n"
                s = ","
                resp += s.join(["{}:{}".format(lvl, logging.getLevelName(lvl)) for lvl in ava_log_lvl])
                resp += "\n"
                resp += "no change made"
                self.logger.info("no change made")
            elif msg[1] == "userip":
              try:
                if len(msg) < 3:
                  resp = "usage: set userip <user name> [new ip]"
                else:
                  username = msg[2]
                  try:
                    if len(msg) < 4:
                      self.dbconn.updateUserIP(username, '')
                    else:
                      userip = msg[3]
                      self.dbconn.updateUserIP(username, userip)
                  except Exception as e:
                    resp = "error when update db: {}".format(e)
                    self.logger.error(resp)
                  else:
                    resp = "Done"
                    self.logger.info("config updated")
              except Exception as e:
                resp = str(e)
          elif msg[0] == "disconnect":
            try:
              if len(msg) < 2:
                resp = "usage: disconnect <remote_ip:remote_port>"
              else:
                remote_host = msg[1]
                remote_ip, remote_port = util.decodeHostPort(remote_host)
                remote_host_hex = util.toHexHost(remote_ip, remote_port)

                self.tunnel_dict_lock.acquire()
                tunnel = self.tunnel_dict[remote_host_hex]
                self.tunnel_dict_lock.release()
                
                self.sendDisconnectRemote(tunnel.controller_host, tunnel.target_host, "disconnected by system")
                self.logger.info("system discconnected {}".format())
                resp = "system disconnected: {}".format(util.decodeHostPort(remote_host))
            except Exception as e:
              resp = "error: {}\n".format(e)
              resp += "usage: disconnect <remote_ip:remote_port>"
          else :
            resp = "unknown command"
            self.logger.info("unknown command")
          control_socket.sendall(resp.encode())

        except Exception as e:
          self.logger.info("unknown command")
          control_socket.sendall("exception: {}".format(e).encode())

      else:
        self.logger.info("disconnected from control")
        self.control_selector.unregister(control_socket)
        control_socket.close()

    except Exception as e:
      self.logger.error("error when receiving from control: {}".format(e))
      self.control_selector.unregister(control_socket)
      control_socket.close()

  ##------------
  def controlThread(self, listen_ip, listen_port):
    self.logger.info("started")
    
    created_sock = False
    while not created_sock:
      try:
        self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control_socket.bind((listen_ip, listen_port))
        self.control_socket.setblocking(False)
        self.control_socket.listen(1)
      except Exception as e:
        self.logger.error("error when binding control socket: {}".format(e))
        self.logger.error("retry after 5s")
        time.sleep(5)
      else:
        self.logger.info("proxy control socket binded successfully")
        created_sock = True

    self.logger.info("listening: {}:{}".format(listen_ip, listen_port))
    self.control_selector = selectors.DefaultSelector()      
    self.control_selector.register(self.control_socket, selectors.EVENT_READ, self.acceptControl)
    
    while self.running:
      try:
        events = self.control_selector.select(timeout=5)
        for key, mask in events:
          callback = key.data
          callback(key.fileobj, mask)
      except Exception as e:
        self.logger.critical("error in selector: {}".format(e))
    self.control_selector.unregister(self.control_socket)
    self.logger.info("ended")

  ##------------
  def start(self):
    self.config = self.readConfig(self.configfile)
    self.routing = self.readRoutingTable(self.routingfile)
    self.proxy_whitelist = self.config['proxy_client_whitelist']
    
    self.logger.info("starting threads...")
    controlThread = threading.Thread(target=self.controlThread, name="controlThread", args=[self.control_ip, self.control_port])
    proxyRecvThread = threading.Thread(target=self.proxyRecvThread, name="proxyRecvThread")
    proxySendThread = threading.Thread(target=self.proxySendThread, name="proxySendThread")
    remoteRecvThreadPool = []
    for routing in self.routing:
      (listen_ip, listen_port) = routing["listen"]
      (target_ip, target_port) = routing["target"]
      name = "remoteRecvThread - {}".format(listen_port)
      t = threading.Thread(target=self.remoteRecvThread, args=[listen_ip, listen_port, target_ip, target_port], name=name)
      remoteRecvThreadPool.append(t)
    remoteSendThread = threading.Thread(target=self.remoteSendThread, name="remoteSendThread")

    controlThread.start()
    proxyRecvThread.start()    
    proxySendThread.start()
    for t in remoteRecvThreadPool:
      t.start()
    remoteSendThread.start()

    controlThread.join()
    proxyRecvThread.join()
    proxySendThread.join()
    for t in remoteRecvThreadPool:
      t.join()
    remoteSendThread.join()

    self.logger.info("bye")


###############################################################################
# main
def main(args):
  loglvl = int(args.verbose) if args.verbose else None
  control = args.control if args.control else "0.0.0.0:4430"
  proxy = args.proxy if args.proxy else "0.0.0.0:8443"
  routing = args.routing if args.routing else "routingtable.conf"
  config = args.config if args.config else "config.json"
  logpath = args.log if args.log else None
  dbpath = args.db if args.db else "./HTTPMonitor/instance/flaskr.sqlite"

  server = ProxyServer(proxy, control, dbpath=dbpath, routingfile=routing, configfile=config, loglvl=loglvl, logpath=logpath)
  
  # signal.signal(signal.SIGQUIT, server.shutdown)
  signal.signal(signal.SIGINT, server.shutdown)

  server.start()



if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-t", "--routing", help="proxy server routing table")
  parser.add_argument("-c", "--config", help="proxy server config")
  parser.add_argument("-p", "--proxy", help="Proxy server binding address and port <ip:port>")
  parser.add_argument("-C", "--control", help="Control server bliding address and port <ip:port>")
  ava_log_lvl = [logging.NOTSET, logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL]
  parser.add_argument("-v", "--verbose", type=int, default=logging.INFO, help=", ".join(["{}:{}".format(lvl, logging.getLevelName(lvl)) for lvl in ava_log_lvl]))
  parser.add_argument("-l", "--log", help="path of the log file")
  parser.add_argument("-d", "--db", help="path of the database")
  
  args = parser.parse_args()

  main(args)