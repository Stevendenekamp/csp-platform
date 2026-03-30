import secrets

# Genereer een veilige random secret key
secret_key = secrets.token_urlsafe(64)
print(f"SECRET_KEY={secret_key}")
print("\nKopieer deze waarde naar je .env bestand!")
