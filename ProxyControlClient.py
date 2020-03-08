# Echo server program
import threading
import queue
import socket
import select
import sys

inputs = [sys.stdin]
connecting_sockets = []
running = True
sock = None
server_ip = None
server_port = None

print("started")
while running:
  read, write, error = select.select(inputs, connecting_sockets, inputs)

  for s in read:
    if s == sys.stdin:
      enter = sys.stdin.readline().strip()
      if enter == "exit":
        if sock:
          sock.shutdown(socket.SHUT_RDWR)
          sock.close()
          sock = None
        running = False

      elif enter.startswith('conn'):
        if sock:
          print("already connected to {}".foramt(sock.getpeername()))
          continue
        enters = enter.split(" ")
        if len(enters) < 3 or not enters[2].isdigit() :
          print("conn <ip> <port>")
          print("connectin to default 127.0.0.1 4430")
          server_ip = "127.0.0.1"
          server_port = 4430
        else:
          server_ip = enters[1]
          server_port = int(enters[2])
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(0)
        rtn = sock.connect_ex((server_ip, server_port))
        connecting_sockets.append(sock)
        print("connecting...:{}: {}".format(rtn, socket.errno.errorcode[rtn]))
        
      elif enter == 'close':
        if sock:
          inputs.remove(sock)
          sock.shutdown(socket.SHUT_RDWR)
          sock.close()
          sock = None
          print("disconnected")
        else:
          print("has not connected")

      elif enter.startswith("sd"):
        data = str(enter[3:]).encode()
        sock.sendall(data)
      else:
        print("unknown command: {}".format(enter))
    else:
      s = s.recv(1024)
      print("recv: {}".format(s.decode()))
  for s in write:
    # print(s.getpeername())
    status = s.connect_ex((server_ip, server_port))
    connecting_sockets.remove(s)
    if status == 0:
      print("connected")
      inputs.append(s)
    else:
      print(s)
      print("connection error: {}: {}".format(status, socket.errno.errorcode[status]))
    pass
  for s in error:
    print("error {}".format(s))
    inputs.remove(sock)
    s.close()
    pass

print("end")
