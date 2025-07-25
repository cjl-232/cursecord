from datetime import datetime
from typing import Annotated

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from pydantic import AfterValidator, BeforeValidator, Field

from schema_components.validators import (
    validate_key_input,
    validate_key_list_input,
    validate_signature_input,
    validate_timestamp_input,
    validate_key_output,
    validate_signature_output,
)

type Base64Key = Annotated[
    str,
    Field(
        title='Base64-Encoded Key',
        description='The Base64 representation of a 32-byte value.',
        max_length=44,
        min_length=44,
    ),
    BeforeValidator(validate_key_input),
]


type Base64KeyList = Annotated[
    list[str],
    Field(
        default=None,
        title='Base64-Encoded Key List',
        description='A list of Base64 representations of 32-byte values.',
    ),
    BeforeValidator(validate_key_list_input),
]


type Base64Signature = Annotated[
    str,
    Field(
        title='Base64-Encoded Signature',
        description='The Base64 representation of a 64-byte signature.',
        max_length=88,
        min_length=88,
    ),
    BeforeValidator(validate_signature_input),
]


type FernetKey = Annotated[
    Fernet,
    BeforeValidator(lambda x: validate_key_output(x, Fernet)),
]


type PrivateExchangeKey = Annotated[
    X25519PrivateKey,
    BeforeValidator(lambda x: validate_key_output(x, X25519PrivateKey)),
]


type PublicExchangeKey = Annotated[
    X25519PublicKey,
    BeforeValidator(lambda x: validate_key_output(x, X25519PublicKey)),
]


type VerificationKey = Annotated[
    Ed25519PublicKey,
    BeforeValidator(lambda x: validate_key_output(x, Ed25519PublicKey)),
]


type RawSignature = Annotated[
    bytes,
    BeforeValidator(validate_signature_output),
]


type Timestamp = Annotated[
    datetime,
    AfterValidator(validate_timestamp_input),
]
