import hashlib

import base58

api_key = "9Rm4xY7d8Hketc5egqyHeQDkoJ8VvjERd7odiTRvaeJ6"
# Hash the API key using SHA-512 to produce 64 bytes.
hashed = hashlib.sha512(api_key.encode("utf-8")).digest()
# Encode the 64 bytes using base58.
encoded_key = base58.b58encode(hashed).decode("utf-8")
print("Your 88-character base58-encoded key:")
print(encoded_key)
print("Length:", len(encoded_key))
