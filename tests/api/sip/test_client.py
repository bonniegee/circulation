"""Standalone tests of the SIP2 client."""
import os
import socket
import ssl

import pytest

from api.sip.client import MockSIPClient, SIPClient
from api.sip.dialect import AutoGraphicsVerso, GenericILS


class MockSocket:
    def __init__(self, *args, **kwargs):
        self.data = b""
        self.args = args
        self.kwargs = kwargs
        self.timeout = None
        self.connected_to = None

    def queue_data(self, new_data):
        if isinstance(new_data, str):
            new_data = new_data.encode("cp850")
        self.data += new_data

    def connect(self, server_and_port):
        self.connected_to = server_and_port

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        block = self.data[:size]
        self.data = self.data[size:]
        return block


class MockWrapSocket:
    def __init__(self):
        self.called_with = None

    def __call__(self, connection, **kwargs):
        self.called_with = (connection, kwargs)
        return connection


class TestSIPClient:
    """Test the real SIPClient class without allowing it to make
    network connections.
    """

    def test_connect(self):
        target_server = object()
        sip = SIPClient(target_server, 999)

        old_socket = socket.socket

        # Mock the socket.socket function.
        socket.socket = MockSocket

        # Call connect() and make sure timeout is set properly.
        try:
            sip.connect()
            assert 12 == sip.connection.timeout
        finally:
            # Un-mock the socket.socket function
            socket.socket = old_socket

    def test_secure_connect(self):

        target_server = object()
        insecure = SIPClient(target_server, 999, use_ssl=False)
        no_cert = SIPClient(target_server, 999, use_ssl=True)
        with_cert = SIPClient(target_server, 999, ssl_cert="cert", ssl_key="key")

        # Mock the socket.socket function.
        old_socket = socket.socket
        socket.socket = MockSocket

        # Mock the ssl.wrap_socket function
        old_wrap_socket = ssl.wrap_socket
        wrap_socket = MockWrapSocket()
        ssl.wrap_socket = wrap_socket

        try:
            # When an insecure connection is created, wrap_socket is
            # not called.
            insecure.connect()
            assert None == wrap_socket.called_with

            # When a secure connection is created with no SSL
            # certificate, wrap_socket() is called on the connection
            # (in this case, a MockSocket), but no other arguments are
            # passed in to wrap_socket().
            no_cert.connect()
            connection, kwargs = wrap_socket.called_with
            assert isinstance(connection, MockSocket)
            assert dict(keyfile=None, certfile=None) == kwargs

            # When a secure connection is created with an SSL
            # certificate, the certificate and key are written to
            # temporary files, and the paths to those files are passed
            # in along with the collection to wrap_socket().
            wrap_socket.called_with = None
            with_cert.connect()
            connection, kwargs = wrap_socket.called_with
            assert isinstance(connection, MockSocket)
            assert {"keyfile", "certfile"} == set(kwargs.keys())
            for tmpfile in list(kwargs.values()):
                tmpfile = os.path.abspath(tmpfile)
                assert os.path.basename(tmpfile).startswith("tmp")
                # By the time the SSL socket has been wrapped, the
                # temporary file has already been removed.  Because of
                # that we can't verify from within a unit test that the
                # correct contents were written to the file.
                assert not os.path.exists(tmpfile)
        finally:
            # Un-mock the old functions.
            socket.socket = old_socket
            ssl.wrap_socket = old_wrap_socket

    def test_read_message(self):
        target_server = object()
        sip = SIPClient(target_server, 999)

        old_socket = socket.socket

        # Mock the socket.socket function.
        socket.socket = MockSocket

        try:
            sip.connect()
            conn = sip.connection

            # Queue bytestrings and read them.
            for data in (
                # Simple message.
                b"abcd\n",
                # Message that contains non-ASCII characters.
                "LE CARRÉ, JOHN\r".encode("cp850"),
                # Message that spans multiple blocks.
                (b"a" * 4097) + b"\n",
            ):
                conn.queue_data(data)
                assert data == sip.read_message()

            # IOError on a message that's too large.
            conn.queue_data("too big\n")
            with pytest.raises(IOError, match="SIP2 response too large."):
                sip.read_message(max_size=2)

            # IOError if transmission stops without ending on a newline.
            conn.queue_data("no newline")
            with pytest.raises(IOError, match="No data read from socket."):
                sip.read_message()
        finally:
            # Un-mock the socket.socket function
            socket.socket = old_socket


class TestBasicProtocol:
    def test_login_message(self):
        sip = MockSIPClient()
        message = sip.login_message("user_id", "password")
        assert "9300CNuser_id|COpassword" == message

    def test_append_checksum(self):
        sip = MockSIPClient()
        sip.sequence_number = 7
        data = "some data"
        new_data = sip.append_checksum(data)
        assert "some data|AY7AZFAAA" == new_data

    def test_sequence_number_increment(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")
        sip.sequence_number = 0
        sip.queue_response("941")
        response = sip.login()
        assert 1 == sip.sequence_number

        # Test wraparound from 9 to 0
        sip.sequence_number = 9
        sip.queue_response("941")
        response = sip.login()
        assert 0 == sip.sequence_number

    def test_resend(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")
        # The first response will be a request to resend the original message.
        sip.queue_response("96")
        # The second response will indicate a successful login.
        sip.queue_response("941")

        response = sip.login()

        # We made two requests for a single login command.
        req1, req2 = sip.requests
        # The first request includes a sequence ID field, "AY", with
        # the value "0".
        assert b"9300CNuser_id|COpassword|AY0AZF556\r" == req1

        # The second request does not include a sequence ID field. As
        # a consequence its checksum is different.
        assert b"9300CNuser_id|COpassword|AZF620\r" == req2

        # The login request eventually succeeded.
        assert {"login_ok": "1", "_status": "94"} == response

    def test_maximum_resend(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")

        # We will keep sending retry messages until we reach the maximum
        sip.queue_response("96")
        sip.queue_response("96")
        sip.queue_response("96")
        sip.queue_response("96")
        sip.queue_response("96")

        # After reaching the maximum the client should give an IOError
        pytest.raises(IOError, sip.login)

        # We should send as many requests as we are allowed retries
        assert sip.MAXIMUM_RETRIES == len(sip.requests)


class TestLogin:
    def test_login_success(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")
        sip.queue_response("941")
        response = sip.login()
        assert {"login_ok": "1", "_status": "94"} == response

    def test_login_password_is_optional(self):
        """You can specify a login_id without specifying a login_password."""
        sip = MockSIPClient(login_user_id="user_id")
        sip.queue_response("941")
        response = sip.login()
        assert {"login_ok": "1", "_status": "94"} == response

    def test_login_failure(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")
        sip.queue_response("940")
        pytest.raises(IOError, sip.login)

    def test_login_happens_when_user_id_and_password_specified(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")
        # We're not logged in, and we must log in before sending a real
        # message.
        assert True == sip.must_log_in

        sip.queue_response("941")
        sip.queue_response(
            "64Y                201610050000114734                        AOnypl |AA12345|AENo Name|BLN|AFYour library card number cannot be located.  Please see a staff member for assistance.|AY1AZC9DE"
        )
        sip.login()
        response = sip.patron_information("patron_identifier")

        # Two requests were made.
        assert 2 == len(sip.requests)
        assert 2 == sip.sequence_number

        # We ended up with the right data.
        assert "12345" == response["patron_identifier"]

    def test_no_login_when_user_id_and_password_not_specified(self):
        sip = MockSIPClient()
        assert False == sip.must_log_in

        sip.queue_response(
            "64Y                201610050000114734                        AOnypl |AA12345|AENo Name|BLN|AFYour library card number cannot be located.  Please see a staff member for assistance.|AY1AZC9DE"
        )
        sip.login()

        # Zero requests made
        assert 0 == len(sip.requests)
        assert 0 == sip.sequence_number

        response = sip.patron_information("patron_identifier")

        # One request made.
        assert 1 == len(sip.requests)
        assert 1 == sip.sequence_number

        # We ended up with the right data.
        assert "12345" == response["patron_identifier"]

    def test_login_failure_interrupts_other_request(self):
        sip = MockSIPClient(login_user_id="user_id", login_password="password")
        sip.queue_response("940")

        # We don't even get a chance to make the patron information request
        # because our login attempt fails.
        pytest.raises(IOError, sip.patron_information, "patron_identifier")

    def test_login_does_not_happen_implicitly_when_user_id_and_password_not_specified(
        self,
    ):
        sip = MockSIPClient()

        # We're implicitly logged in.
        assert False == sip.must_log_in

        sip.queue_response(
            "64Y                201610050000114734                        AOnypl |AA12345|AENo Name|BLN|AFYour library card number cannot be located.  Please see a staff member for assistance.|AY1AZC9DE"
        )
        response = sip.patron_information("patron_identifier")

        # One request was made.
        assert 1 == len(sip.requests)
        assert 1 == sip.sequence_number

        # We ended up with the right data.
        assert "12345" == response["patron_identifier"]


class TestPatronResponse:
    def setup_method(self):
        self.sip = MockSIPClient()

    def test_incorrect_card_number(self):
        self.sip.queue_response(
            "64Y                201610050000114734                        AOnypl |AA240|AENo Name|BLN|AFYour library card number cannot be located.|AY1AZC9DE"
        )
        response = self.sip.patron_information("identifier")

        # Test some of the basic fields.
        assert response["institution_id"] == "nypl "
        assert response["personal_name"] == "No Name"
        assert response["screen_message"] == [
            "Your library card number cannot be located."
        ]
        assert response["valid_patron"] == "N"
        assert response["patron_status"] == "Y             "
        parsed = response["patron_status_parsed"]
        assert True == parsed["charge privileges denied"]
        assert False == parsed["too many items charged"]

    def test_hold_items(self):
        "A patron has multiple items on hold."
        self.sip.queue_response(
            "64              000201610050000114837000300020002000000000000AOnypl |AA233|AEBAR, FOO|BZ0030|CA0050|CB0050|BLY|CQY|BV0|CC15.00|AS123|AS456|AS789|BEFOO@BAR.COM|AY1AZC848"
        )
        response = self.sip.patron_information("identifier")
        assert "0003" == response["hold_items_count"]
        assert ["123", "456", "789"] == response["hold_items"]

    def test_multiple_screen_messages(self):
        self.sip.queue_response(
            "64Y  YYYYYYYYYYY000201610050000115040000000000000000000000000AOnypl |AA233|AESHELDON, ALICE|BZ0030|CA0050|CB0050|BLY|CQN|BV0|CC15.00|AFInvalid PIN entered.  Please try again or see a staff member for assistance.|AFThere are unresolved issues with your account.  Please see a staff member for assistance.|AY2AZ9B64"
        )
        response = self.sip.patron_information("identifier")
        assert 2 == len(response["screen_message"])

    def test_extension_field_captured(self):
        """This SIP2 message includes an extension field with the code XI."""
        self.sip.queue_response(
            "64  Y           00020161005    122942000000000000000000000000AA240|AEBooth Active Test|BHUSD|BDAdult Circ Desk 1 Newtown, CT USA 06470|AQNEWTWN|BLY|CQN|PA20191004|PCAdult|PIAllowed|XI86371|AOBiblioTest|ZZfoo|AY2AZ0000"
        )
        response = self.sip.patron_information("identifier")

        # The Evergreen XI field is a known extension and is picked up
        # as sipserver_internal_id.
        assert "86371" == response["sipserver_internal_id"]

        # The ZZ field is an unknown extension and is captured under
        # its SIP code.
        assert ["foo"] == response["ZZ"]

    def test_variant_encoding(self):
        response_unicode = "64              000201610210000142637000000000000000000000000AOnypl |AA12345|AELE CARRÉ, JOHN|BZ0030|CA0050|CB0050|BLY|CQY|BV0|CC15.00|BEfoo@example.com|AY1AZD1B7\r"

        # By default, we expect data from a SIP2 server to be encoded
        # as CP850.
        assert "cp850" == self.sip.encoding
        self.sip.queue_response(response_unicode.encode("cp850"))
        response = self.sip.patron_information("identifier")
        assert "LE CARRÉ, JOHN" == response["personal_name"]

        # But a SIP2 server may send some other encoding, such as
        # UTF-8. This can cause odd results if the circulation manager
        # tries to parse the data as CP850.
        self.sip.queue_response(response_unicode.encode("utf-8"))
        response = self.sip.patron_information("identifier")
        assert "LE CARR├ë, JOHN" == response["personal_name"]

        # Giving SIPClient the right encoding means the data is
        # converted correctly.
        sip = MockSIPClient(encoding="utf-8")
        assert "utf-8" == sip.encoding
        sip.queue_response(response_unicode.encode("utf-8"))
        response = sip.patron_information("identifier")
        assert "LE CARRÉ, JOHN" == response["personal_name"]

    def test_embedded_pipe(self):
        """In most cases we can handle data even if it contains embedded
        instances of the separator character.
        """
        self.sip.queue_response(
            "64              000201610050000134405000000000000000000000000AOnypl |AA12345|AERICHARDSON, LEONARD|BZ0030|CA0050|CB0050|BLY|CQY|BV0|CC15.00|BEleona|rdr@|bar.com|AY1AZD1BB\r"
        )
        response = self.sip.patron_information("identifier")
        assert "leona|rdr@|bar.com" == response["email_address"]

    def test_different_separator(self):
        """When you create the SIPClient you get to specify which character
        to use as the field separator.
        """
        sip = MockSIPClient(separator="^")
        sip.queue_response(
            "64Y                201610050000114734                        AOnypl ^AA240^AENo Name^BLN^AFYour library card number cannot be located.^AY1AZC9DE"
        )
        response = sip.patron_information("identifier")
        assert "240" == response["patron_identifier"]

    def test_location_code_is_optional(self):
        """You can specify a location_code when logging in, or not."""
        without_code = self.sip.login_message("login_id", "login_password")
        assert without_code.endswith("COlogin_password")
        with_code = self.sip.login_message(
            "login_id", "login_password", "location_code"
        )
        assert with_code.endswith("COlogin_password|CPlocation_code")

    def test_institution_id_field_is_always_provided(self):
        without_institution_arg = self.sip.patron_information_request(
            "patron_identifier", "patron_password"
        )
        assert without_institution_arg.startswith("AO|", 33)

    def test_institution_id_field_value_provided(self):
        # Fake value retrieved from DB
        sip = MockSIPClient(institution_id="MAIN")
        with_institution_provided = sip.patron_information_request(
            "patron_identifier", "patron_password"
        )
        assert with_institution_provided.startswith("AOMAIN|", 33)

    def test_patron_password_is_optional(self):
        without_password = self.sip.patron_information_request("patron_identifier")
        assert without_password.endswith("AApatron_identifier|AC")
        with_password = self.sip.patron_information_request(
            "patron_identifier", "patron_password"
        )
        assert with_password.endswith("AApatron_identifier|AC|ADpatron_password")

    def test_parse_patron_status(self):
        m = MockSIPClient.parse_patron_status
        pytest.raises(ValueError, m, None)
        pytest.raises(ValueError, m, "")
        pytest.raises(ValueError, m, " " * 20)
        parsed = m("Y Y Y Y Y Y Y ")
        for yes in [
            "charge privileges denied",
            #'renewal privileges denied',
            "recall privileges denied",
            #'hold privileges denied',
            "card reported lost",
            #'too many items charged',
            "too many items overdue",
            #'too many renewals',
            "too many claims of items returned",
            #'too many items lost',
            "excessive outstanding fines",
            #'excessive outstanding fees',
            "recall overdue",
            #'too many items billed',
        ]:
            assert parsed[yes] == True

        for no in [
            #'charge privileges denied',
            "renewal privileges denied",
            #'recall privileges denied',
            "hold privileges denied",
            #'card reported lost',
            "too many items charged",
            #'too many items overdue',
            "too many renewals",
            #'too many claims of items returned',
            "too many items lost",
            #'excessive outstanding fines',
            "excessive outstanding fees",
            #'recall overdue',
            "too many items billed",
        ]:
            assert parsed[no] == False


class TestClientDialects:
    def setup_method(self):
        self.sip = MockSIPClient()

    def test_generic_dialect(self):
        # Generic ILS should send end_session message
        self.sip.dialect = GenericILS
        self.sip.queue_response("36Y201610210000142637AO3|AA25891000331441|AF|AG")
        self.sip.end_session("username", "password")
        assert self.sip.read_count == 1
        assert self.sip.write_count == 1

    def test_ag_dialect(self):
        # AG VERSO ILS shouldn't end_session message
        self.sip.dialect = AutoGraphicsVerso
        self.sip.end_session("username", "password")
        assert self.sip.read_count == 0
        assert self.sip.write_count == 0
