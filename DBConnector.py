import sqlite3

class DBConnector():

  def __init__(self, path):
    self.path = path
    # conn = sqlite3.connect(path)

  def close(self):
    pass

  def login(self, ip):
    conn = sqlite3.connect(self.path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("select id, username, ip from user where ip=?", (ip,))
    user = cursor.fetchone()

    if user:
      cursor.execute("UPDATE user SET last_login=current_timestamp where id={}".format(user['id']))
      conn.commit()

    conn.close()

    return user

  def updateUserIP(self, username, ip):
    conn = sqlite3.connect(self.path)
    cursor = conn.cursor()
    cursor.execute("UPDATE user SET ip=? where username=?", (ip, username))
    conn.commit()
  

if __name__ == "__main__":
  dbconn = DBConnector("./HTTPMonitor/instance/flaskr.sqlite")
  dbconn.updateUserIP("admin","123.123.123.123")

  user = dbconn.login("123.123.123.123")
  if user:
    print(user['username'])
  else:
    print("user does not exist")