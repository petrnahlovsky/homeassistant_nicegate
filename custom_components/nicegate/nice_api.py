"""API for Nice gate WiFi interface."""
import asyncio
import base64
import binascii
import hashlib
import logging
import random
import re
import ssl
import time

import defusedxml.ElementTree as ET

BUFF_SIZE = 512
_LOGGER = logging.getLogger("nicegate")


class NiceGateApi:
    """API for Nice Gate communication."""

    def __init__(self, host, mac, username, pwd, source=None):
        """Initialize API for Nice gate."""
        self.host = host
        self.target = mac
        # Cloud users (new MyNice app) are identified by controllerID and skip VERIFY
        self.skip_verify = bool(source)
        self.source = source if source else f"python_{username}"
        self.username = username
        self.descr = "Home assistant integration"
        self.pwd = pwd
        # Client challenge, randomly generated
        self.client_challenge = f"{random.randint(1, 9999999):08x}".upper()
        # Server challenge, send by server
        self.server_challenge = ""
        self.command_sequence = 1
        self.command_id = 0
        self.session_id = 1
        self.gate_status = None
        # Hex bitmask of supported T4 commands, reported by IT4WIFI (None = unknown)
        self.t4_allowed: int | None = None
        self.serv_reader: asyncio.StreamReader = None
        self.serv_writer: asyncio.StreamWriter = None
        self._keep_alive_task: asyncio.Task = None
        self._loop_task: asyncio.Task = None
        self.update_callback = None
        # Monotonic timestamp of the last message received from IT4WIFI
        self._last_rx: float = 0.0
        # Guards against reconnect storms
        self._last_connect: float = 0.0
        self._connect_lock = asyncio.Lock()

    def set_update_callback(self, callback):
        """Register callback for update notification."""
        self.update_callback = callback

    async def get_status(self):
        """Get current status of gate."""
        if self.gate_status is None:
            await self.status()
        return self.gate_status

    # Translate hex string to byte array
    def __hex_to_bytearray(self, hex_str):
        return bytes.fromhex(hex_str)

    # Get sha256
    def __sha256(self, *args):
        hsh = hashlib.sha256()
        for arg in args:
            hsh.update(arg)
        return hsh.digest()

    # Invert byte array
    def __invert_array(self, data):
        return data[::-1]

    # Generating command ID from session ID
    def __generate_command_id(self, session_id):
        i = self.command_sequence
        self.command_sequence = i + 1
        return (i << 8) | (int(session_id) & 255)

    # Build sign for message
    def __build_signature(self, xml_command):
        client_challenge = self.__hex_to_bytearray(self.client_challenge)
        server_challenge = self.__hex_to_bytearray(self.server_challenge)

        pairing_password = base64.b64decode(self.pwd)
        session_password = self.__sha256(
            pairing_password,
            self.__invert_array(server_challenge),
            self.__invert_array(client_challenge),
        )

        msg_hash = self.__sha256(xml_command.encode())
        sign = self.__sha256(msg_hash, session_password)
        return "<Sign>" + base64.b64encode(sign).decode("utf-8") + "</Sign>"

    def __get_setup_code_check(self, setup_code:str):
        client_challenge = self.__hex_to_bytearray(self.client_challenge)
        setup_code_check = bytes(setup_code, 'utf-8') + client_challenge[::-1] + bytes("Nice4U",'utf-8')
        crc32 = binascii.crc32(setup_code_check) & 0xFFFFFFFF
        return "{0:08X}".format(crc32)

    # Check if sign needed
    def __is_sign_needed(self, command_type):
        if command_type in ("CONFIG", "VERIFY", "CONNECT", "PAIR"):
            return False
        return True

    # Wrap message, protocol needed
    def __wrap_message(self, xml: str) -> bytes:
        _LOGGER.debug(xml)
        return ("\u0002" + xml + "\u0003").encode()

    async def __keep_alive_loop(self):
        """Ping the module and drop the connection when it stops answering."""
        try:
            while True:
                await asyncio.sleep(60)
                if self._loop_task is None or self._loop_task.done():
                    _LOGGER.warning("Receive loop is not running, dropping connection")
                    break
                if not await self.__ping_alive():
                    _LOGGER.warning("No answer from IT4WIFI, dropping connection")
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Keep alive loop failed")
        finally:
            await self.disconnect()

    async def __ping_alive(self, attempts: int = 2, timeout: int = 15) -> bool:
        """Send STATUS and wait for any incoming message. True if module answered."""
        for attempt in range(attempts):
            before = self._last_rx
            await self.status()
            waited = 0.0
            while waited < timeout:
                await asyncio.sleep(1)
                waited += 1
                if self._last_rx > before:
                    return True
            _LOGGER.debug("No answer within %ss (attempt %s)", timeout, attempt + 1)
        return False

    async def __recvloop(self):
        writer = self.serv_writer
        try:
            while True:
                msg = await self.__recvall()
                if msg == "":
                    break
                await self.__process_event(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Receive loop failed")
        finally:
            if self.serv_writer is writer:
                await self.disconnect()

    # Get all data from socket
    async def __recvall(self, reader=None):
        data = b""
        if reader is None:
            reader=self.serv_reader
        while True:
            try:
                part = await reader.readuntil(b"\x03")
                if part == b"":
                    _LOGGER.error("Disconnected")
                    return ""
            except asyncio.exceptions.IncompleteReadError:
                _LOGGER.error("Disconnected")
                return ""
            except OSError as error_msg:
                # a "real" error occurred
                _LOGGER.error(error_msg)
                return ""
            else:
                data += part
                if re.search(b"\x02", data):
                    data = data[1:]
                if re.search(b"\x03", data):
                    data = data[:-1]
                    _LOGGER.debug(data)
                    break
        answer = data.decode()
        self._last_rx = time.monotonic()
        self.__find_session_id(answer)
        return answer

    def __find_session_id(self, msg):
        """Find Session ID in response, SessionID used for MessageID generating."""
        match = re.search(r'Authentication\sid=[\'"]?([^\'" >]+)', msg)
        if match:
            self.session_id = match.group(1)

    def __find_server_challenge(self, msg):
        """Find server challenge in response, needed of message signature."""
        match = re.search(r'sc=[\'"]?([^\'" >]+)', msg)
        if match:
            self.server_challenge = match.group(1)
        else:
            _LOGGER.warning("No server challenge found")

    def __build_message(self, command_type, body)->bytes:
        """Build request."""
        self.command_id = self.__generate_command_id(self.session_id)
        start_request = '<Request id="{}" source="{}" target="{}" gw="gwID" protocolType="NHK" protocolVersion="1.0" type="{}">\r\n'.format(
            self.command_id, self.source, self.target, command_type
        )
        end_request = "</Request>\r\n"
        msg = self.__wrap_message(
            start_request
            + body
            + (
                self.__build_signature(start_request + body)
                if self.__is_sign_needed(command_type)
                else ""
            )
            + end_request
        )
        return msg


    async def __process_event(self, msg):
        _LOGGER.debug(msg)
        resp = ET.fromstring(msg)
        t4 = resp.find("./Devices/Device/Properties/T4_allowed")
        if t4 is not None and t4.get("values"):
            try:
                self.t4_allowed = int(t4.get("values"), 16)
            except ValueError:
                _LOGGER.debug("Unexpected T4_allowed value %s", t4.get("values"))
        if resp.tag == "Event":
            if resp.attrib.get("type") == "CHANGE":
                self.gate_status = resp.findtext(
                    "./Devices/Device/Properties/DoorStatus"
                )
                _LOGGER.debug("Event CHANGE received %s", self.gate_status)
                if self.update_callback is not None:
                    await self.update_callback()
        if resp.tag == "Response":
            if resp.attrib.get("type") == "STATUS":
                self.gate_status = resp.findtext(
                    "./Devices/Device/Properties/DoorStatus"
                )
                _LOGGER.debug("Status received %s", self.gate_status)
                if self.update_callback is not None:
                    await self.update_callback()

    def __reset_session(self):
        """Reset session state, same as a freshly created API object."""
        self.client_challenge = f"{random.randint(1, 9999999):08x}".upper()
        self.server_challenge = ""
        self.command_sequence = 1
        self.command_id = 0
        self.session_id = 1

    async def _ensure_connected(self) -> bool:
        if self.serv_writer is not None and self.serv_reader is not None:
            if not self.serv_writer.is_closing():
                return True
            _LOGGER.debug("Socket is closing, reconnecting")
            await self.disconnect()
        return await self.connect()

    async def __send(self, command_type, body, retry=True) -> bool:
        """Send a message, reconnect and retry once when the write fails."""
        if not await self._ensure_connected():
            return False
        writer = self.serv_writer
        if writer is None or writer.is_closing():
            if retry:
                await self.disconnect()
                return await self.__send(command_type, body, retry=False)
            return False
        try:
            writer.write(self.__build_message(command_type, body))
            await writer.drain()
            return True
        except Exception as ex:
            _LOGGER.warning("Send of %s failed: %s", command_type, ex)
            await self.disconnect()
            if retry:
                return await self.__send(command_type, body, retry=False)
            return False

    async def pair(self, setup_code:str)->str:
        self.pwd=None
        writer=None
        if self.username is None or self.username == "":
            return None
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
            ctx.check_hostname = False
            ctx.options |= 0x4  # ssl.OP_LEGACY_SERVER_CONNECT
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2

            await asyncio.sleep(0.01)
            reader, writer = await asyncio.open_connection(self.host, 443, ssl=ctx)

            msg=self.__build_message(
                    'PAIR',
                    (f'<Authentication username="{self.username}" cc="{self.client_challenge}" '
                        f'check="{self.__get_setup_code_check(setup_code)}" CType="phone" OSType="Android" '
                        'OSVer="6.0.1" desc="hass integration" />')
                )
            writer.write(msg)
            await writer.drain()
            pair = await self.__recvall(reader)
            match= re.search(r'<Authentication\s+id=[\'"]?([^\'" >]+)[\'"]?\s+username=[\'"]?([^\'" >]+)[\'"]?\s+pwd=[\'"]?([^\'" >]+)[\'"]?', pair)
            if match:
                self.pwd=match.groups()[2]
                _LOGGER.debug(f"User paired. Password {self.pwd}")
            else:
                _LOGGER.warning("No user found")
        except ConnectionError as error_msg:
            _LOGGER.error( error_msg, exc_info=True)
        except TimeoutError:
            _LOGGER.warning("Timeout")
        except Exception as ex:
            _LOGGER.error(ex, exc_info=True)

        if writer is not None:
            writer.close()

        return self.pwd

    async def verify_connect(self)->str:
        status="error"
        writer=None
        if self.username is None or self.username == "":
            return "error"
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
            ctx.check_hostname = False
            ctx.options |= 0x4  # ssl.OP_LEGACY_SERVER_CONNECT
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2

            await asyncio.sleep(0.01)
            reader, writer = await asyncio.open_connection(self.host, 443, ssl=ctx)

            if self.skip_verify:
                msg=self.__build_message(
                    "CONNECT",
                    '<Authentication username="{}" cc="{}"/>'.format(
                        self.username, self.client_challenge
                    ),
                )
                writer.write(msg)
                await writer.drain()
                connect = await self.__recvall(reader)
                if "<Error" in connect or "sc=" not in connect:
                    _LOGGER.warning("CONNECT rejected: %s", connect)
                else:
                    self.__find_server_challenge(connect)
                    status="connect"
                writer.close()
                return status

            msg=self.__build_message("VERIFY", f'<User username="{self.username}"/>')
            writer.write(msg)
            await writer.drain()
            verify = await self.__recvall(reader)
            match=re.search(r'<Authentication\s+id=[\'"]?([^\'" >]+)[\'"]?\s+username=[\'"]?([^\'" >]+)[\'"]?\s+perm=[\'"]?([^\'" >]+)[\'"]?', verify)
            if match:
                perm=match.groups()[2]
                _LOGGER.debug(f"User connected. Status '{perm}'")
                if perm=="wait":
                    status="wait"
                else:
                    msg=self.__build_message(
                        "CONNECT",
                        '<Authentication username="{}" cc="{}"/>'.format(
                            self.username, self.client_challenge
                        ),
                    )
                    writer.write(msg)
                    await writer.drain()
                    connect = await self.__recvall(reader)
                    self.__find_server_challenge(connect)
                    status="connect"
            else:
                _LOGGER.warning("No user found")
        except ConnectionError as error_msg:
            _LOGGER.error( error_msg, exc_info=True)
        except TimeoutError:
            _LOGGER.warning("Timeout")
        except Exception as ex:
            _LOGGER.error(ex, exc_info=True)

        if writer is not None:
            writer.close()

        return status

    async def connect(self):
        """Connect to IT4WIFI."""
        async with self._connect_lock:
            if self.serv_writer is not None and not self.serv_writer.is_closing():
                return True
            since = time.monotonic() - self._last_connect
            if since < 5:
                await asyncio.sleep(5 - since)
            self._last_connect = time.monotonic()
            return await self.__connect()

    async def __connect(self):
        """Open the connection with a fresh session."""
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
            ctx.check_hostname = False
            ctx.options |= 0x4  # ssl.OP_LEGACY_SERVER_CONNECT
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
            ctx.maximum_version = ssl.TLSVersion.TLSv1_2
            
            if self.serv_writer is not None or self.serv_reader is not None:
                await self.disconnect()
            if self._loop_task is not None:
                self._loop_task.cancel()
            if self._keep_alive_task is not None:
                self._keep_alive_task.cancel()

            self.__reset_session()
            await asyncio.sleep(0.01)
            reader, writer = await asyncio.open_connection(self.host, 443, ssl=ctx)
            self.serv_reader = reader
            self.serv_writer = writer
            self._last_rx = time.monotonic()

            if self.skip_verify:
                verify = "Authentication id=0"
            else:
                msg=self.__build_message("VERIFY", f'<User username="{self.username}"/>')
                self.serv_writer.write(msg)
                await self.serv_writer.drain()
                verify = await self.__recvall()
            if re.search(r'Authentication\sid=[\'"]?([^\'" >]+)', verify):
                msg=self.__build_message(
                    "CONNECT",
                    f'<Authentication username="{self.username}" cc="{self.client_challenge}"/>',
                )
                self.serv_writer.write(msg)
                await self.serv_writer.drain()
                connect = await self.__recvall()
                self.__find_server_challenge(connect)
                # start loop
                self._keep_alive_task = asyncio.create_task(self.__keep_alive_loop())
                self._loop_task = asyncio.create_task(self.__recvloop())
                # asyncio.create_task(self.status())
                return True
            _LOGGER.warning("No user found")
        except ConnectionError as error_msg:
            _LOGGER.error( error_msg, exc_info=True)
        except TimeoutError:
            _LOGGER.warning("Timeout")
        except Exception as ex:
            _LOGGER.error(ex, exc_info=True)
        return False

    async def status(self, cmd="STATUS") -> bool:
        """Get IT4WIFI status. Returns False when the message could not be sent."""
        return await self.__send(cmd, "")

    async def info(self, cmd="INFO"):
        """Get IT4WIFI info."""
        await self.__send(cmd, "")

    async def change(self, command):
        """Open, close or stop gates."""
        await self.__send(
            "CHANGE",
            f'<Devices><Device id="1">\n<Services><DoorAction>{command}</DoorAction>\n</Services ></Device></Devices>',
        )

    async def t4(self, code: str):
        """Send T4 command (e.g. MDAx = step by step)."""
        await self.__send(
            "CHANGE",
            f'<Devices><Device id="1">\n<Services><T4Action>{code}</T4Action>\n</Services ></Device></Devices>',
        )

    def t4_supported(self, bit: int) -> bool:
        """Return True if T4 command is supported or support is unknown."""
        if self.t4_allowed is None:
            return True
        return bool(self.t4_allowed & (1 << bit))

    async def check(self):
        """Ping for prevent sokcet close."""
        await self.__send(
            "CHECK",
            f'<Authentication id="{self.session_id}" username="{self.username}"/>',
        )

    async def disconnect(self):
        """Disconnect from IT4WIFI."""
        self.command_id = 0
        self.command_sequence = 1
        if self.serv_writer is not None:
            try:
                self.serv_writer.close()
            except Exception as ex:
                _LOGGER.debug("Closing the socket failed: %s", ex)
        self.serv_writer = None
        self.serv_reader = None

    async def shutdown(self):
        """Stop background tasks and close the connection."""
        current = asyncio.current_task()
        for task in (self._keep_alive_task, self._loop_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        self._keep_alive_task = None
        self._loop_task = None
        await self.disconnect()
