import argparse
import pathlib
import os
import json
import base64
import win32crypt  # TODO: Separate DPAPI from the rest of the script
import uuid
import struct

# from Crypto.Hash import MD4
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.hashes import Hash, SHA256, SHA512, SHA1

VERSION = "1.0"

AUX_KEY_PREFIX = "DPAPI"

DEC_KEY_PREFIX = "v10"

DPAPI_BLOB_GUID = uuid.UUID("df9d8cd0-1501-11d1-8c7a-00c04fc297eb")


# AES-256-GCM decryption
def aes_256_gcm_decrypt(key, nonce, ciphertext, tag):
    decryptor = Cipher(algorithms.AES(key), modes.GCM(nonce, tag), backend=default_backend()).decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


# PBKDF2 key derivation
def pbkdf2_derive_key(algorithm, password, salt, iterations, key_length):
    kdf = PBKDF2HMAC(
        algorithm=algorithm, length=key_length, salt=salt, iterations=iterations, backend=default_backend()
    )
    return kdf.derive(password)


# Hashing algorithm
def hash_algorithm(data, algorithm, rounds=1):
    for _ in range(rounds):
        digest = Hash(algorithm, backend=default_backend())
        digest.update(data)
        data = digest.finalize()
    return data


# SHA-256 hash
def hash_sha256(data, rounds=1):
    return hash_algorithm(data, SHA256(), rounds)


# SHA-512 hash
def hash_sha512(data, rounds=1):
    return hash_algorithm(data, SHA512(), rounds)


# SHA-1 hash
def hash_sha1(data, rounds=1):
    return hash_algorithm(data, SHA1(), rounds)


# MD4 hash
# def hash_md4(data):
#    return MD4.new(data).digest()


# def hash_from_alg_id(data, alg_id, rounds=1):
#    if alg_id == 32780:
#        return hash_sha256(data, rounds)
#    elif alg_id == 32782:
#        return hash_sha512(data, rounds)
#    else:
#        raise ValueError(f"Unsupported hash algorithm ID: {alg_id}")


def get_hash_algorithm(alg_id):
    if alg_id == 32780:
        return SHA256()
    elif alg_id == 32782:
        return SHA512()
    else:
        raise ValueError(f"Unsupported hash algorithm ID: {alg_id}")


# Parse command line arguments
def parse_args():
    parser = argparse.ArgumentParser(
        prog="SignalDecryptor",
        description="Decrypts the forensic artifacts from Signal Desktop on Windows",
        usage="""%(prog)s [-m auto] -d <signal_dir> -o <output_dir> [OPTIONS]
        %(prog)s -m aux -d <signal_dir> -o <output_dir> [-kf <file> | -k <HEX>] [OPTIONS]
        %(prog)s -m key -d <signal_dir> -o <output_dir> [-kf <file> | -k <HEX>] [OPTIONS]
        %(prog)s -m manual -d <signal_dir> -o <output_dir> -wS <SID> -wP <password> [OPTIONS]
        """,
    )  # TODO: Better usage message
    # [-d <signal_dir> | (-c <file> -ls <file>)]

    # Informational arguments
    parser.add_argument(
        "-V",
        "--version",
        help="Print the version of the script",
        action="version",
        version=f"%(prog)s {VERSION}",
    )

    # Custom function to parse mode argument
    def parse_mode(value):
        aliases = {
            "auto": "auto",
            "manual": "manual",
            "aux": "aux",
            "key": "key",
            "a": "auto",
            "m": "manual",
            "ak": "aux",
            "dk": "key",
        }
        normalized_value = value.lower()
        if normalized_value not in aliases:
            raise argparse.ArgumentTypeError(f"Invalid mode '{value}'. Valid choices are: {', '.join(aliases.keys())}")
        return aliases[normalized_value]

    # Custom type function to convert HEX to bytes
    def hex_to_bytes(value):
        value = value.replace(" ", "").lower()
        try:
            return bytes.fromhex(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"Invalid HEX string: {value}")

    # Define mode argument
    parser.add_argument(
        "-m",
        "--mode",
        help=(
            "Mode of operation (choices: 'auto' for Windows Auto, 'aux' for Auxiliary Key Provided, "
            "'key' for Decryption Key Provided), 'manual' for Windows Manual. "
            "Short aliases: -mA (Auto), -mAK (Auxiliary Key), -mDK (Decryption Key), -mM (Manual)"
            "Default: auto"
        ),
        type=parse_mode,
        choices=["auto", "aux", "key", "manual"],
        metavar="{auto|aux|key|manual}",
        default="auto",
    )

    # IO arguments
    io_group = parser.add_argument_group(
        "Input/Output",
        "Arguments related to input/output paths. Output directory and either Signal's directory or configuration and local state files are required.",
    )
    io_group.add_argument(
        "-d", "--dir", help="Path to Signal's Roaming directory", type=pathlib.Path, metavar="<dir>", required=True
    )  # TODO: Change Roaming to other stuff
    io_group.add_argument(
        "-o",
        "--output",
        help="Path to the output directory",
        type=pathlib.Path,
        metavar="<dir>",
        required=True,
    )
    # io_group.add_argument(
    #    "-c", "--config", help="Path to the Signal's configuration file", type=pathlib.Path, metavar="<file>"
    # )
    # io_group.add_argument(
    #    "-ls", "--local-state", help="Path to the Signal's Local State file", type=pathlib.Path, metavar="<file>"
    # )

    # Provided key related arguments
    key_group = parser.add_argument_group(
        "Key Provided Modes", "Arguments available for both Key Provided modes."
    ).add_mutually_exclusive_group()
    key_group.add_argument(
        "-kf",
        "--key-file",
        help="Path to the file containing the HEX encoded key as a string",
        type=pathlib.Path,
        metavar="<file>",
    )
    key_group.add_argument("-k", "--key", help="Key in HEX format", type=hex_to_bytes, metavar="<HEX>")

    # DPAPI related arguments
    # manual_group = parser.add_argument_group("Windows Manual Mode", "Arguments required for manual mode.")
    # manual_group.add_argument("-wS", "--windows-sid", help="Target windows user's SID", metavar="<SID>")
    # manual_group.add_argument("-wP", "--windows-password", help="Target windows user's password", metavar="<password>")

    # Operational arguments
    skip_group = parser.add_mutually_exclusive_group()
    skip_group.add_argument("-sD", "--skip-decryption", help="Skip all artifact decryption", action="store_true")
    skip_group.add_argument("-sA", "--skip-attachments", help="Skip attachment decryption", action="store_true")

    # Verbosity arguments
    verbosity_group = parser.add_mutually_exclusive_group()
    verbosity_group.add_argument("-v", "--verbose", help="Enable verbose output", action="count", default=0)
    verbosity_group.add_argument("-q", "--quiet", help="Enable quiet output", action="store_true")

    # Parse arguments
    return parser.parse_args()


# Validate arguments
def validate_args(args: argparse.Namespace):

    # Validate Signal directory
    if not args.dir.is_dir():
        raise FileNotFoundError(f"Signal directory '{args.dir}' does not exist or is not a directory.")
    else:
        args.config = args.dir / "config.json"
        args.local_state = args.dir / "Local State"

        # Check for Signal's configuration file
        if not args.config.is_file():
            raise FileNotFoundError(f"Signal's configuration file '{args.config}' does not exist or is not a file.")

        # Check for Signal's local state file
        if not args.local_state.is_file():
            raise FileNotFoundError(f"Signal's local state file '{args.local_state}' does not exist or is not a file.")

    # Validate output directory
    if not args.output.is_dir():
        try:
            os.makedirs(args.output)
        except OSError as e:
            raise FileNotFoundError(f"Output directory '{args.output}' does not exist and could not be created.") from e

    # Validate manual mode arguments
    if args.mode == "manual":
        if not args.windows_sid:
            raise ValueError("Windows User SID is required for manual mode.")
        if not args.windows_password:
            raise ValueError("Windows User Password is required for manual mode.")

    # Validate key provided mode arguments
    if args.mode in ["aux", "key"]:
        if args.key_file:
            if not args.key_file.is_file():
                raise FileNotFoundError(f"Key file '{args.key_file}' does not exist or is not a file.")
        elif not args.key:
            raise ValueError("A key is required for Key Provided modes.")

    # If mode is Key Provided and skip decryption is enabled, raise an error
    if args.mode == "key" and args.skip_decryption:
        raise ValueError("Decryption cannot be skipped when providing the decryption key.")


class MalformedInputFileError(Exception):
    """Exception raised for a malformed input file."""

    pass


class MalformedKeyError(Exception):
    """Exception raised for a malformed key."""

    pass


def unprotect_with_dpapi(data: bytes):
    log("Unprotecting the auxiliary key through DPAPI...", 1)
    try:
        _, decrypted_data = win32crypt.CryptUnprotectData(data)
        return decrypted_data
    except Exception as e:
        raise ValueError("Failed to unprotect the auxiliary key with DPAPI.") from e


def process_dpapi_blob(data: bytes):
    try:
        log("Extracting data from DPAPI BLOB...", 2)
        master_key_guid = str(uuid.UUID(bytes_le=data[24:40]))
        log(f"> Master Key GUID: {master_key_guid}", 3)
        desc_len = struct.unpack("<I", data[44:48])[0]
        idx = 48 + desc_len + 8
        salt_len = struct.unpack("<I", data[idx : idx + 4])[0]
        idx += 4
        salt = data[idx : idx + salt_len]
        log(f"> BLOB Salt: {bytes_to_hex(salt)}", 3)
        idx += salt_len
        hmac_key_len = struct.unpack("<I", data[idx : idx + 4])[0]
        idx += 4 + hmac_key_len + 8
        hmac_key_len = struct.unpack("<I", data[idx : idx + 4])[0]
        idx += 4 + hmac_key_len
        data_len = struct.unpack("<I", data[idx : idx + 4])[0]
        idx += 4
        cipher_data = data[idx : idx + data_len]
        log(f"> Cipher Data: {bytes_to_hex(cipher_data)}", 3)
        return master_key_guid, salt, cipher_data
    except Exception as e:
        raise MalformedKeyError("Failed to extract information from the auxiliary key blob.") from e


def process_dpapi_master_key_file(master_key_path: pathlib.Path):
    # TODO: Better exception here
    if not master_key_path.is_file():
        raise FileNotFoundError(f"Master Key file '{master_key_path}' does not exist or is not a file.")
    log("Reading from the master key file...", 3)
    with master_key_path.open("rb") as f:
        data = f.read()
    log("Processing the master key file...", 2)

    idx = 96
    master_key_len = struct.unpack("<Q", data[idx : idx + 8])[0]
    idx += 8 + 24 + 4
    salt = data[idx : idx + 16]
    log(f"> Master Key Salt: {bytes_to_hex(salt)}", 3)
    idx += 16
    rounds = struct.unpack("<I", data[idx : idx + 4])[0]
    log(f"> Rounds: {rounds}", 3)
    idx += 4
    hash_alg_id = struct.unpack("<I", data[idx : idx + 4])[0]
    log(f"> Algorithm Hash ID: {hash_alg_id}", 3)
    idx += 4 + 4
    encrypted_master_key = data[idx : idx + master_key_len - 32]  # NOTE: No idea why -32, but it works
    log(f"> Encrypted Master Key: {bytes_to_hex(encrypted_master_key)}", 3)
    return salt, rounds, hash_alg_id, master_key_len, encrypted_master_key


def unprotect_manually(data: bytes, sid: str, password: str):
    try:
        log("Unprotecting the auxiliary key manually...", 1)
        master_key_guid, blob_salt, cipher_data = process_dpapi_blob(data)
        log("Crafting the master key path...", 2)
        master_key_path = pathlib.Path(os.getenv("APPDATA")) / "Microsoft" / "Protect" / sid / master_key_guid
        log(f"> Master Key Path: {master_key_path}", 3)
        mk_salt, hash_rounds, hash_alg_id, mk_len, encrypted_master_key = process_dpapi_master_key_file(master_key_path)
        log("Deriving the master key's encription key...", 2)
        hash_alg = get_hash_algorithm(hash_alg_id)
        nt_hash = hash_sha1(password.encode("utf-16le"))
        log(f"> NT Hash: {bytes_to_hex(nt_hash)}", 3)
        mk_encryption_key = pbkdf2_derive_key(hash_alg, nt_hash, mk_salt, hash_rounds, 32)
        log(f"> Master Key Encryption Key: {bytes_to_hex(mk_encryption_key)}", 3)
        log("Decrypting the master key...", 2)

        # TODO: List requirements for manual acquisition of Aux Key?

        raise NotImplementedError("Manual mode is not implemented yet.")
    except Exception as e:
        raise MalformedKeyError("Failed to unprotect the auxiliary key manually.") from e


def fetch_key_from_args(args: argparse.Namespace):
    # If a key file is provided, read the key from the file
    if args.key_file:
        log("Reading the key from the file...", 2)
        with args.key_file.open("r") as f:
            return bytes.fromhex(f.read().strip())
    return args.key


def fetch_aux_key(args: argparse.Namespace):
    # If the user provided the auxiliary key, return it
    if args.mode == "aux":
        return fetch_key_from_args(args)
    else:
        with args.local_state.open("r") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                raise MalformedInputFileError("The Local State file was malformed: Invalid JSON structure.")

            # Validate the presence of "os_crypt" and "encrypted_key"
            encrypted_key = data.get("os_crypt", {}).get("encrypted_key")
            if not encrypted_key:
                raise MalformedInputFileError(
                    "The Local State file was malformed: Missing the encrypted auxiliary key."
                )

            # Decode the base64 encoded key and remove the prefix
            try:
                encrypted_key = base64.b64decode(encrypted_key)[len(AUX_KEY_PREFIX) :]
            except ValueError:
                raise MalformedKeyError("The encrypted key is not a valid base64 string.")
            except IndexError:
                raise MalformedKeyError("The encrypted key is malformed.")

            # Check if this is a DPAPI blob
            if encrypted_key[4:20] != DPAPI_BLOB_GUID.bytes_le:
                raise MalformedKeyError("The encrypted auxiliary key is not in the expected DPAPI BLOB format.")

            if args.mode == "auto":
                return unprotect_with_dpapi(encrypted_key)
            elif args.mode == "manual":
                return unprotect_manually(encrypted_key, args.windows_sid, args.windows_password)
    return None


def fetch_decryption_key(args: argparse.Namespace, aux_key: bytes):
    with args.config.open("r") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            raise MalformedInputFileError("The Configuration file was malformed: Invalid JSON structure.")

        # Validate the presence of "encryptedKey"
        encrypted_key = data.get("encryptedKey")
        if not encrypted_key:
            raise MalformedInputFileError("The Configuration file was malformed: Missing the encrypted decryption key.")

        # Import the hex string into bytes
        try:
            key = bytes.fromhex(encrypted_key)
        except ValueError:
            raise MalformedKeyError("The encrypted decryption key is not a valid HEX string.")

        # Check if the key has the expected prefix
        if key[: len(DEC_KEY_PREFIX)] != DEC_KEY_PREFIX.encode("utf-8"):
            raise MalformedKeyError("The encrypted decryption key does not start with the expected prefix.")
        key = key[len(DEC_KEY_PREFIX) :]

        log("Processing the encrypted decryption key...", 2)

        nonce = key[:12]  # Nonce is in the first 12 bytes
        gcm_tag = key[-16:]  # GCM tag is in the last 16 bytes
        key = key[12:-16]

        log(f"> Nonce: {bytes_to_hex(nonce)}", 3)
        log(f"> GCM Tag: {bytes_to_hex(gcm_tag)}", 3)
        log(f"> Key: {bytes_to_hex(key)}", 3)

        log("Decrypting the decryption key...", 2)
        decrypted_key = aes_256_gcm_decrypt(aux_key, nonce, key, gcm_tag)

        return bytes.fromhex(decrypted_key.decode("utf-8"))

    return None


def bytes_to_hex(data: bytes):
    return "".join(f"{b:02x}" for b in data)


quiet = False
verbose = 0


def log(message: str, level: int = 0):
    if not quiet and (verbose >= level):
        print(message)


def main():
    # Parse and validate arguments
    args = parse_args()
    validate_args(args)

    # Setup logging
    global quiet, verbose
    quiet = args.quiet
    verbose = args.verbose

    # Initialize decryption key
    decryption_key = None

    # Fetch the decryption key
    if args.mode == "key":
        log("Fetching decryption key...", 1)
        decryption_key = fetch_key_from_args(args)
        log(f"> Decryption Key: {bytes_to_hex(decryption_key)}", 2)
        print("[i] Loaded decryption key")
    else:
        log("Fetching auxiliary key...", 1)
        aux_key = fetch_aux_key(args)
        if len(aux_key) != 32:
            raise MalformedKeyError("The auxiliary key is not 32 bytes long.")
        log(f"> Auxiliary Key: {bytes_to_hex(aux_key)}", 2)
        print("[i] Loaded auxiliary key")

        print("[i] Decrypting the decryption key...")
        decryption_key = fetch_decryption_key(args, aux_key)
        log(f"> Decryption Key: {bytes_to_hex(decryption_key)}", 2)
        print("[i] Loaded decryption key")

    # ....
    return


if __name__ == "__main__":
    main()
