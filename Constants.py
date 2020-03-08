
MAX_REMOTE = 20
BUFFER_SIZE = 8192
CONNECTION_TIMEOUT = 60
RECONNECT_INTERVAL = 5  # reconnect to proxy server interval in second

# if there are no message sending to or receiving from proxy server > HEART_BIT_INTERVAL seoncds,
# a heart beat message will be sent to the other side. an echo is expected
HEART_BEAT_INTERVAL = 50

MIN_MSG_LEN = 19

LOG_MSG_SIZE = 40

CMD_GREETING          = b"GRT"
CMD_FORWARD           = b"FWD"
CMD_REMOTE_DISCONNECT = b"DIS"
CMD_CONTROL           = b'CTL'
CMD_ALV               = b'ALV'  # TCP keep alive message
