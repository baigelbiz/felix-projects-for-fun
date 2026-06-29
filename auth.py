import hashlib
import hmac

USERS = {
    "alice": hashlib.sha256(b"wonderland").hexdigest(),
    "bob": hashlib.sha256(b"builder").hexdigest(),
}


def authenticate(username: str, password: str) -> bool:
    """Return True if the username/password pair is valid."""
    stored_hash = USERS.get(username)
    if stored_hash is None:
        return False
    provided_hash = hashlib.sha256(password.encode()).hexdigest()
    return hmac.compare_digest(stored_hash, provided_hash)


if __name__ == "__main__":
    assert authenticate("alice", "wonderland")
    assert not authenticate("alice", "wrong-password"), "BUG: wrong password accepted!"
    assert not authenticate("eve", "anything")
    print("All auth checks passed.")
