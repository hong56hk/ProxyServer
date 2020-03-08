import functools

from flask import (
  Blueprint, flash, g, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

from flaskr.db import get_db
import socket
import json

bp = Blueprint('home', __name__)

PROXY_CONTROL_IP = "192.168.13.11"
PROXY_CONTROL_PORT = 4430


@bp.route('/', methods=('GET', 'POST'))
def index():
  if request.method == 'GET':
    return render_template('index.html')
  else:
    db = get_db()
    resp = ''
    data = json.loads(request.data.decode())
    error = None

    if 'name' not in data:
      resp = 'name is required.'
    elif 'code' not in data:
      resp = 'code is required.'
    else:
      name = data['name'].lower()
      code = data['code']
      if len(name) > 15 or len(code) > 15:# or [c in name for c in "!@#$%^&*()_+|"]:
        resp = "Invalid nput"
      elif db.execute('SELECT id FROM user WHERE username = ? and password = ?', (data['name'], data['code'])).fetchone() is None:
        resp = 'Failed'
      else:
        try:
          msg = "set userip {} {}".format(data['name'], request.remote_addr)

          sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
          sock.settimeout(10)
          sock.connect_ex((PROXY_CONTROL_IP, PROXY_CONTROL_PORT))
          sock.sendall(msg.encode())

          resp = sock.recv(1024).decode()
          sock.shutdown(socket.SHUT_RDWR)
          sock.close()
            
        except Exception as e:
          error = str(e)
          print("error when connecting to proxy control server: {}".format(error))
          resp = 'error: ' + error
          
    return resp
