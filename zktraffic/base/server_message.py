# ==================================================================================================
# Copyright 2014 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================


from collections import defaultdict

from .util import (
  INT_STRUCT,
  parent_path,
  read_bool,
  read_buffer,
  read_int_bool_int,
  read_int_int,
  read_int_int_long,
  read_number,
  read_reply_header,
  read_string,
  StringTooLong,
)
from .zookeeper import (
  DeserializationError,
  MultiHeader,
  OpCodes,
  PING_XID,
  WATCH_XID,
)


class ServerMessageType(type):
  TYPES = {}
  OPCODE = None

  def __new__(cls, clsname, bases, dct):
    obj = super(ServerMessageType, cls).__new__(cls, clsname, bases, dct)
    if obj.OPCODE in cls.TYPES:
      raise ValueError("Duplicate class/opcode name: %s" % obj.OPCODE)
    else:
      if obj.OPCODE is not None:
        cls.TYPES[obj.OPCODE] = obj
      return obj

  @classmethod
  def get(cls, key, default=None):
    return cls.TYPES.get(key, default)


class ServerMessage(ServerMessageType('ClientMessageType', (object,), {})):
  __slots__ = ("timestamp", "xid", "zxid", "error", "path", "client", "auth", "server")

  def __init__(self, xid, zxid, error, path, client, server):
    self.timestamp = 0  # this will be set by caller later on
    self.auth = ""      # ditto
    self.xid = xid
    self.zxid = zxid
    self.error = error
    self.path = path
    self.client = client
    self.server = server

  def parent_path(self, level):
    return parent_path(self.path, level)

  @property
  def name(self):
    return "%s%s" % ("" if self.error == 0 else "Failed", self.__class__.__name__)

  @property
  def opcode(self):
    return self.OPCODE

  @classmethod
  def with_params(cls, xid, zxid, error, data, offset, client, server):
    """Build a ServerMessage (a reply or event) with the given params, possibly parsing some more.

    This must be overridden by ServerMessage subclasses to offer extra parsing of
    parameters specific to the ServerMessage subclass.

    :param xid: the transaction id associated with this ServerMessage
    :param zxid: the ZooKeeper txn id associated with this ServerMessage
    :param error: if the response has an error
    :param data: the remaining data of the associated data packet
    :param offset: the offset from which the data should be read
    :param client: the ip:port to which this ServerMessage is directed to

    :returns: Returns an instance of the specific ServerMessage subclass.
    :raises DeserializationError: if parsing the ServerMessage fails.
    """

    return cls(xid, zxid, error, "", client, server)

  @classmethod
  def from_payload(cls, data, client, server, requests_xids):
    """
    requests_xids is a dict of xid and type of prev seen client requests
    """
    reply_size, offset = read_number(data, 0)
    if reply_size <= 0:
      raise DeserializationError("Bad reply length: %d" % (reply_size))

    (xid, zxid, err), offset = read_reply_header(data, offset)
    handler = cls.handler_for(xid, requests_xids)
    if handler:
      return handler.with_params(xid, zxid, err, data, offset, client, server)

    raise DeserializationError("No handler for xid=%s" % (xid))

  @classmethod
  def handler_for(cls, xid, requests_xids):
    """
    Watch events are generated by the server so there are no requests for them. Also,
    for efficiency ping requests - which happen every 1/3 of the session timeout - aren't
    saved. So we special case both.
    """
    if xid == WATCH_XID:
      return WatchEvent
    elif xid == PING_XID:
      return PingReply

    request_type = requests_xids.pop(xid, None)
    return ServerMessageType.get(request_type, None)

  def __str__(self):
    return "%s(xid=%s, zxid=%s, error=%s, server=%s)\n" % (
        self.name, self.xid, self.zxid, self.error, self.server)


class WatchEvent(ServerMessage):
  OPCODE = None

  def __init__(self, event_type, state, path, client, server):
    self.event_type = event_type
    self.state = state
    super(WatchEvent, self).__init__(-1, -1, 0, path, client, server)

  @classmethod
  def with_params(cls, xid, zxid, error, data, offset, client, server):
    (event_type, state), offset = read_int_int(data, offset)
    try:
      path, offset = read_string(data, offset)
    except StringTooLong:
      path = "path-too-long"
    return cls(event_type, state, path, client, server)

  EVENT_TYPES = defaultdict(lambda: "UnknownWatchEvent", {
    -1: "None",
    1: "NodeCreated",
    2: "NodeDeleted",
    3: "NodeDataChanged",
    4: "NodeChildrenChanged",
  })

  @property
  def name(self):
    return self.EVENT_TYPES[self.event_type]

  def __str__(self):
    return "Event%s(state=%s, path=%s, client=%s, server=%s)\n" % (
      self.name, self.state, self.path, self.client, self.server)


class Reply(ServerMessage):
  pass


class ConnectReply(Reply):
  OPCODE = OpCodes.CONNECT

  def __init__(self, protocol, timeout, session, passwd, readonly, client, server):
    self.protocol = protocol
    self.timeout = timeout
    self.session = session
    self.passwd = passwd
    self.readonly = readonly

    super(ConnectReply, self).__init__(0, -1, 0, "", client, server)

  @classmethod
  def with_params(cls, xid, zxid, error, data, offset, client, server):
    """
    ConnectReply is special and doesn't have a ReplyHeader so we rewind the offset
    back to the start (i.e.: right after the reply size)
    """
    offset = INT_STRUCT.size

    (protocol, timeout, session), offset = read_int_int_long(data, offset)
    passwd, offset = read_buffer(data, offset)
    readonly, offset = read_bool(data, offset)
    return cls(protocol, timeout, session, passwd, readonly, client, server)

  @property
  def name(self):
    return super(ConnectReply, self).name if self.timeout > 0 else "SessionExpiration"

  def __str__(self):
    return "%s(ver=%s, timeout=%s, session=0x%x, readonly=%s, server=%s)\n" % (
      self.name, self.protocol, self.timeout, self.session, self.readonly, self.server)


class PingReply(Reply):
  OPCODE = OpCodes.PING


class AuthReply(Reply):
  OPCODE = OpCodes.SETAUTH


class GetDataReply(Reply):
  OPCODE = OpCodes.GETDATA


class ExistsReply(Reply):
  OPCODE = OpCodes.EXISTS


class SyncReply(Reply):
  OPCODE = OpCodes.SYNC


class SetDataReply(Reply):
  OPCODE = OpCodes.SETDATA


class DeleteReply(Reply):
  OPCODE = OpCodes.DELETE


class GetChildrenReply(Reply):
  OPCODE = OpCodes.GETCHILDREN

  def __init__(self, xid, zxid, error, count, client, server):
    self.count = count
    super(GetChildrenReply, self).__init__(xid, zxid, error, "", client, server)

  @classmethod
  def with_params(cls, xid, zxid, error, data, offset, client, server):
    count, _ = read_number(data, offset) if error == 0 else (0, 0)
    return cls(xid, zxid, error, count, client, server)

  def __str__(self):
    return "%s(xid=%s, zxid=%s, error=%s, count=%s, server=%s)\n" % (
      self.name, self.xid, self.zxid, self.error, self.count, self.server)


class GetChildren2Reply(GetChildrenReply):
  OPCODE = OpCodes.GETCHILDREN2


class CreateReply(Reply):
  OPCODE = OpCodes.CREATE

  @classmethod
  def with_params(cls, xid, zxid, error, data, offset, client, server):
    try:
      path, offset = read_string(data, offset) if error == 0 else ("", offset)
    except StringTooLong:
      path = "path-too-long"
    return cls(xid, zxid, error, path, client, server)

  def __str__(self):
    return "%s(xid=%s, zxid=%s, error=%s, path=%s, server=%s)\n" % (
      self.name, self.xid, self.zxid, self.error, self.path, self.server)


class Create2Reply(CreateReply):
  OPCODE = OpCodes.CREATE2


class SetWatchesReply(Reply):
  OPCODE = OpCodes.SETWATCHES


class MultiReply(Reply):
  OPCODE = OpCodes.MULTI

  def __init__(self, xid, zxid, error, client, first_header, server):
    self.headers = [first_header]
    super(MultiReply, self).__init__(xid, zxid, error, "", client, server)

  @classmethod
  def with_params(cls, xid, zxid, error, data, offset, client, server):
    (first_opcode, done, err), _ = read_int_bool_int(data, offset)
    return cls(xid, zxid, error, client, MultiHeader(first_opcode, done, err), server)

  def __str__(self):
    return "%s(xid=%s, zxid=%s, error=%s, header=%s, server=%s)\n" % (
      self.name, self.xid, self.zxid, self.error, self.headers[0], self.server)
