'''
version: 20200208

'''
import threading
import selectors
import datetime
import argparse
import random
import select
import socket
import queue
import json
import time
import time
import sys
import os
import re

MAX_REMOTE = 10
BUFFER_SIZE = 4096
RECONNECT_INTERVAL = 5  # reconnect to proxy server interval in second
MIN_MSG_LEN = 19

LOG_DESC = ["FATAL", "ERROR", "INFOR", "DEBUG", "TRACE"]
LOGLEVEL_FATAL = 0
LOGLEVEL_ERROR = 1
LOGLEVEL_INFOR = 2
LOGLEVEL_DEBUG = 3
LOGLEVEL_TRACE = 4
LOG_MSG_SIZE = 40

CMD_GREETING          = b"GRT"
CMD_FORWARD           = b"FWD"
CMD_REMOTE_DISCONNECT = b"DIS"
CMD_CONTROL           = b'CTL'

  
###############################################################################
##-------------
# return ip and port in hex format
def toHexHost(ip, port):
  if re.match("^[\d]*.[\d]*.[\d]*.[\d]*", ip) and (0 < port <= 65535) :
    hosta = ip.split(".")
    return bytes([int(hosta[0]), int(hosta[1]), int(hosta[2]), int(hosta[3]), int(port >> 8), int(port & 255)])
  return None

# convert ip and port in hex format to string ip and int port
def fromHexHost(host):
  ip = str(host[0]) + "." + str(host[1]) + "." + str(host[2]) + "." + str(host[3])
  port = ( host[4] << 8 ) + (host[5])
  return ip, port

# litte endian
def shortToHex(aNum):
  return bytes([(aNum >> 8), int(aNum & 255)])

# litte endian
def shortFromHex(aNum):
  return (aNum[0] << 8 ) + (aNum[1])

# litte endian
def intToHex(aNum):
  return bytes([(aNum >> 24) & 255, (aNum >> 16) & 255, (aNum >> 8) & 255, int(aNum & 255)])

# litte endian
def intFromHex(aNum):
  return (aNum[0] << 24 ) + (aNum[1] << 16 ) + (aNum[2] << 8 ) + (aNum[3])

def decodeHostPort(hostport):
  result = (None, None)
  if hostport and len(hostport) > 1:
    if re.match("^[\d]*.[\d]*.[\d]*.[\d]*:[\d]*$", hostport):
      info = hostport.split(":")
      host = info[0].strip()
      port = info[1].strip()
      result = (host, int(port))
  return result

##------------
# cmd is bytes
# from_host = hex format of from_host
# to_host = hex format of to_host
# data is bytes array
def encodeMessage(seq, cmd, from_host, to_host, data=None):
  data_size_hex = b''
  msg = b''
  hex_msg_id = shortToHex(seq%65535)

  # from_to = toHexHost(from_host[0], from_host[1]) + toHexHost(to_host[0], to_host[1])
  from_to = from_host + to_host
  if data:
    data_size_hex = shortToHex(len(hex_msg_id) + len(cmd) + len(from_to) + len(data))
    msg = data_size_hex + hex_msg_id + cmd + from_to + data
  else:
    data_size_hex = shortToHex(len(hex_msg_id) + len(cmd) + len(from_to))
    msg = data_size_hex + hex_msg_id + cmd + from_to
  return msg

def decodeMessage(data):
  seq = data[0:2]
  cmd = data[2:5]
  from_host = data[5:11]
  to_host = data[11:17]
  payload = data[17:]
  return (shortFromHex(seq), cmd, from_host, to_host, payload)

