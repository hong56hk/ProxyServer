class Tunnel:
  def __init__(self, controller_host, target_host, socket):
    self.controller_host = controller_host # bytes
    self.target_host = target_host # bytes
    self.socket = socket

