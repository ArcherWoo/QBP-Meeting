import logging
from pathlib import Path


logger = logging.getLogger(__name__)


def decrypt_attachment(file_path):
    path = Path(file_path)
    logger.info("Attachment decryption requested for %s", path)

    # TODO: Call the intranet decryption server with requests once the
    # service IP, port, authentication, and response contract are finalized.
    # The future implementation can return either this same path after
    # in-place replacement or a separate plaintext file path for callers to
    # copy over the encrypted upload.
    return path
