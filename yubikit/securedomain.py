from .core import Tlv, int2bytes, BadResponseError
from .core.smartcard import (
    AID,
    SmartCardConnection,
    SmartCardProtocol,
    ApduError,
    SW,
    ScpProcessor,
)
from .core.smartcard.scp import (
    INS_INITIALIZE_UPDATE,
    INS_EXTERNAL_AUTHENTICATE,
    INS_INTERNAL_AUTHENTICATE,
    INS_PERFORM_SECURITY_OPERATION,
    KeyRef,
    ScpKeyParams,
    StaticKeys,
)

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.asymmetric import ec
from dataclasses import dataclass
from typing import Mapping, Sequence, Union, cast
from enum import IntEnum, unique


import logging

logger = logging.getLogger(__name__)


INS_GET_DATA = 0xCA
INS_PUT_KEY = 0xD8
INS_STORE_DATA = 0xE2
INS_DELETE = 0xE4
INS_GENERATE_KEY = 0xF1


@unique
class KeyType(IntEnum):
    AES = 0x88
    ECC_PUBLIC_KEY = 0xB0
    ECC_PRIVATE_KEY = 0xB1
    ECC_KEY_PARAMS = 0xF0


_DEFAULT_KCV_IV = b"\1" * 16


@unique
class Curve(IntEnum):
    SECP256R1 = 0x00
    SECP384R1 = 0x01
    SECP521R1 = 0x02
    BrainpoolP256R1 = 0x03
    BrainpoolP384R1 = 0x05
    BrainpoolP512R1 = 0x07

    @classmethod
    def _from_key(cls, private_key: ec.EllipticCurvePrivateKey) -> "Curve":
        name = private_key.curve.name.lower()
        for curve in cls:
            if curve.name.lower() == name:
                return curve
        raise ValueError("Unsupported private key")

    @property
    def _curve(self) -> ec.EllipticCurve:
        return getattr(ec, self.name)()


@dataclass
class KeyInformation:
    key: KeyRef
    components: Mapping[int, int]

    @classmethod
    def parse(cls, data: bytes) -> "KeyInformation":
        return cls(
            KeyRef(data[:2]),
            dict(zip(data[2::2], data[3::2])),
        )


@dataclass
class CaIssuer:
    identifier: bytes
    key: KeyRef

    @classmethod
    def parse_list(cls, data: bytes) -> Sequence["CaIssuer"]:
        tlvs = Tlv.parse_list(data)
        return [
            cls(tlvs[i].value, KeyRef(tlvs[i + 1].value))
            for i in range(0, len(tlvs), 2)
        ]


def _int2asn1(value: int) -> bytes:
    bs = int2bytes(value)
    if bs[0] & 0x80:
        bs = b"\x00" + bs
    return Tlv(0x93, bs)


def _encrypt_cbc(key: bytes, data: bytes, iv: bytes = b"\0" * 16) -> bytes:
    encryptor = Cipher(
        algorithms.AES(key),
        # TODO: modes.CBC(iv),
        modes.ECB(),  # nosec
        backend=default_backend(),
    ).encryptor()
    return encryptor.update(data) + encryptor.finalize()


class SecureDomainSession:
    """A session for managing SCP keys"""

    def __init__(self, connection: SmartCardConnection):
        self.protocol = SmartCardProtocol(connection)
        self.protocol.select(AID.SECURE_DOMAIN)
        logger.debug("SecureDomain session initialized")

    def authenticate(self, key_params: ScpKeyParams) -> None:
        """Initialize SCP and authenticate the session.

        SCP11b does not authenticate the OCE, and will not allow the usage of commands
        which require authentication of the OCE.
        """
        self.protocol.init_scp(key_params)

    def get_data(self, tag: int, data: bytes = b"") -> bytes:
        """Read data from the secure domain."""
        return self.protocol.send_apdu(0, INS_GET_DATA, tag >> 8, tag & 0xFF, data)

    def get_key_information(self) -> Sequence[KeyInformation]:
        """Get information about the currently loaded keys."""
        return [
            KeyInformation.parse(Tlv.unpack(0xC0, d))
            for d in Tlv.parse_list(self.get_data(0xE0))
        ]

    def get_card_recognition_data(self) -> bytes:
        """Get information about the card."""
        return Tlv.unpack(0x73, self.get_data(0x66))

    def get_supported_ca_identifiers(self, kloc: bool = False) -> Sequence[CaIssuer]:
        """Get a list of the CA issuer Subject Key Identifiers for keys.

        By default this will get the KLCC CA list.
        Set kloc = True to instead get the KLOC list.
        """
        try:
            return CaIssuer.parse_list(self.get_data(0xFF33 if kloc else 0xFF34))
        except ApduError as e:
            if e.sw == SW.REFERENCE_DATA_NOT_FOUND:
                return []
            raise

    def get_certificate_bundle(self, key: KeyRef) -> Sequence[x509.Certificate]:
        """Get the certificates associated with the given SCP11 private key.

        Certificates are returned leaf-last.
        """
        return [
            x509.load_der_x509_certificate(cert)
            for cert in Tlv.parse_list(self.get_data(0xBF21, Tlv(0xA6, Tlv(0x83, key))))
        ]

    def reset(self) -> None:
        """Perform a factory reset of the Secure Domain.

        This will remove all keys and associated data, as well as restore the default
        SCP03 static keys, and generate a new (attestable) SCP11b key.
        """
        logger.debug("Resetting all SCP keys")
        # Reset is done by blocking all available keys
        data = b"\0" * 8
        for key_info in self.get_key_information():
            key = key_info.key
            if key.kid == 0x01:
                # SCP03 uses KID=0, we use KVN=0 to allow deleting the default keys
                # which have an invalid KVN (0xff).
                key = KeyRef(0, 0)
                ins = INS_INITIALIZE_UPDATE
            elif key.kid in (0x02, 0x03):
                continue  # Skip these, will be deleted by 0x01
            elif key.kid in (0x11, 0x15):
                ins = INS_EXTERNAL_AUTHENTICATE
            elif key.kid == 0x13:
                ins = INS_INTERNAL_AUTHENTICATE
            else:  # 10, 20-2F
                ins = INS_PERFORM_SECURITY_OPERATION

            for _ in range(65):
                try:
                    self.protocol.send_apdu(0x80, ins, key.kvn, key.kid, data)
                except ApduError as e:
                    if e.sw in (
                        SW.AUTH_METHOD_BLOCKED,
                        SW.SECURITY_CONDITION_NOT_SATISFIED,
                    ):
                        break
                    elif e.sw == SW.INCORRECT_PARAMETERS:
                        continue
                    raise
        logger.info("SCP keys reset")

    def store_data(self, data: bytes) -> None:
        """Stores data in the secure domain.

        Requires OCE verification.
        """
        self.protocol.send_apdu(0, INS_STORE_DATA, 0x90, 0, data)

    def store_certificate_bundle(
        self, key: KeyRef, certificates: Sequence[x509.Certificate]
    ) -> None:
        """Store the certificate chain for the given key.

        Requires OCE verification.

        Certificates should be in order, with the leaf certificate last.
        """
        logger.debug(f"Storing certificate bundle for {key}")
        self.store_data(
            Tlv(0xA6, Tlv(0x83, key))
            + Tlv(
                0xBF21,
                b"".join(
                    c.public_bytes(serialization.Encoding.DER) for c in certificates
                ),
            )
        )
        logger.info("Certificate bundle stored")

    def store_allow_list(self, key: KeyRef, serials: Sequence[int]) -> None:
        """Store which certificate serial numbers that can be used for a given key.

        Requires OCE verification.

        If no allowlist is stored, any certificate signed by the CA can be used.
        """
        logger.debug(f"Storing serial allowlist for {key}")
        self.store_data(
            Tlv(0xA6, Tlv(0x83, key))
            + Tlv(0x70, b"".join(_int2asn1(s) for s in serials))
        )
        logger.info("Serial allowlist stored")

    def store_ca_issuer(self, key: KeyRef, ski: bytes, klcc: bool = False) -> None:
        """Store the SKI (Subject Key Identifier) for the CA of a given key.

        Requires OCE verification.

        By default stores the CA for a KLOC. Set klcc = True to store for the KLCC.
        """
        logger.debug(f"Storing CA issuer SKI for {key}: {ski.hex()}")
        self.store_data(
            Tlv(
                0xA6,
                Tlv(0x80, b"\1" if klcc else b"\0") + Tlv(0x42, ski) + Tlv(0x83, key),
            )
        )
        logger.info("CA issuer SKI stored")

    def delete_key(self, kid: int, kvn: int, delete_last: bool = False) -> None:
        """Delete one (or more) keys.

        Requires OCE verification.

        All keys matching the given KID and/or KVN will be deleted.
        To delete the final key you must set delete_last = True.
        """
        if not kid and not kvn:
            raise ValueError("Must specify at least one of kid, kvn.")

        logger.debug(f"Deleting keys with KID={kid or 'ANY'}, KVN={kvn or 'ANY'}")
        data = b""
        if kid:
            data += Tlv(0xD0, bytes([kid]))
        if kvn:
            data += Tlv(0xD2, bytes([kvn]))
        self.protocol.send_apdu(0x80, INS_DELETE, 0, int(delete_last), data)
        logger.info("Keys deleted")

    def generate_ec_key(
        self, key: KeyRef, curve: Curve = Curve.SECP256R1, replace_kvn: int = 0
    ) -> ec.EllipticCurvePublicKey:
        """Generate a new SCP11 key.

        Requires OCE verification.

        Use replace_kvn to replace an existing key.
        """
        logger.debug(
            f"Generating new key for {key}"
            + (f", replacing KVN={replace_kvn}" if replace_kvn else "")
        )
        data = bytes([key.kvn]) + Tlv(KeyType.ECC_KEY_PARAMS, bytes([curve]))
        resp = self.protocol.send_apdu(
            0x80, INS_GENERATE_KEY, replace_kvn, key.kid, data
        )
        encoded_point = Tlv.unpack(KeyType.ECC_PUBLIC_KEY, resp)
        logger.info("New key generated")
        return ec.EllipticCurvePublicKey.from_encoded_point(curve._curve, encoded_point)

    def put_key(
        self,
        key: KeyRef,
        sk: Union[StaticKeys, ec.EllipticCurvePrivateKey, ec.EllipticCurvePublicKey],
        replace_kvn: int = 0,
    ) -> None:
        """Import an SCP key.

        Requires OCE verification.

        The value of the sk argument should match the SCP type as defined by the KID.
        Use replace_kvn to replace an existing key.
        """
        logger.debug(f"Importing key into {key} of type {type(sk)}")
        processor = self.protocol._processor
        if not isinstance(processor, ScpProcessor):
            raise ValueError("Must be authenticated!")

        data = bytes([key.kvn])
        expected = data
        dek = processor._state._keys.key_dek
        p2 = key.kid
        if isinstance(sk, StaticKeys):
            if not dek:
                raise ValueError("No session DEK key available")
            if not sk.key_dek:
                raise ValueError("New DEK must be set in static keys")
            p2 |= 0x80
            for k in cast(Sequence[bytes], sk):
                kcv = _encrypt_cbc(k, _DEFAULT_KCV_IV)[:3]
                data += Tlv(KeyType.AES, _encrypt_cbc(dek, k)) + bytes([len(kcv)]) + kcv
                expected += kcv
        else:
            if isinstance(sk, ec.EllipticCurvePrivateKey):
                if not dek:
                    raise ValueError("No session DEK key available")
                n = (sk.key_size + 7) // 8
                s = int2bytes(sk.private_numbers().private_value, n)
                data += Tlv(KeyType.ECC_PRIVATE_KEY, _encrypt_cbc(dek, s))
            elif isinstance(sk, ec.EllipticCurvePublicKey):
                data += Tlv(
                    KeyType.ECC_PUBLIC_KEY,
                    sk.public_bytes(
                        serialization.Encoding.X962,
                        serialization.PublicFormat.UncompressedPoint,
                    ),
                )
            else:
                raise TypeError("Unsupported key type")
            data += Tlv(KeyType.ECC_KEY_PARAMS, bytes([Curve._from_key(sk)])) + b"\0"

        resp = self.protocol.send_apdu(0x80, INS_PUT_KEY, replace_kvn, p2, data)
        if resp != expected:
            raise BadResponseError("Incorrect key check value")
        logger.info("Key imported")
