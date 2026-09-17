"""Fixed Google identity contract; provider subjects are never email addresses."""

GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_ISSUER_ALIASES = (GOOGLE_ISSUER, "accounts.google.com")
GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_SUBJECT_PREFIX = "oidc:v1:https://accounts.google.com:"
_MAX_GOOGLE_SUBJECT_LENGTH = 255


def google_subject_binding(subject: str) -> str:
    """Encode one already verified Google subject without normalization or email linking."""
    if not 1 <= len(subject) <= _MAX_GOOGLE_SUBJECT_LENGTH or any(
        not "!" <= character <= "~" for character in subject
    ):
        raise ValueError("Google subject must be bounded printable ASCII without whitespace")
    return GOOGLE_SUBJECT_PREFIX + subject
