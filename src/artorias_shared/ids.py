"""Infrastructure-independent identity generation."""
import uuid

def uid():
    return uuid.uuid4().hex
