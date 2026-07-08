"""Тесты secret scanner (ADR-0006, §12): паттерны, entropy, denylist, гейт.

Все «секреты» в тестах — заведомо ненастоящие: публичный AWS example-ключ
и синтетические токены из повторяющихся символов. Реальных секретов в
репозитории быть не должно (см. правила проекта).
"""

from svarog_harness.secrets import (
    gitignore_block,
    is_secret_path,
    scan_files,
    scan_text,
    shannon_entropy,
)

# Публичный документированный AWS example (не настоящий ключ).
_FAKE_AWS = "AKIAIOSFODNN7EXAMPLE"
_FAKE_GH = "ghp_" + "A1b2C3d4" * 5  # 40 символов, синтетический
_FAKE_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----"


def test_detects_aws_key() -> None:
    findings = scan_text(f"aws_key = {_FAKE_AWS}")
    assert any(f.rule == "aws-access-key-id" for f in findings)
    # Значение вырезано (redaction, §12).
    assert all(_FAKE_AWS not in f.excerpt for f in findings)


def test_detects_github_token() -> None:
    findings = scan_text(f"export TOKEN={_FAKE_GH}")
    assert any(f.rule == "github-token" for f in findings)


def test_detects_private_key_block() -> None:
    findings = scan_text(_FAKE_PRIVATE_KEY)
    assert any(f.rule == "private-key-block" for f in findings)


def test_detects_high_entropy_assignment() -> None:
    findings = scan_text("api_key = 'Zx9Kq2Lm7Pw4Rt6Yn1Bv3Hj8'")
    assert any(f.rule == "high-entropy-assignment" for f in findings)


def test_ignores_placeholder_assignment() -> None:
    assert scan_text("password = changeme") == []
    assert scan_text("api_key = your_api_key_here") == []
    assert scan_text("token = ${GITHUB_TOKEN}") == []


def test_ignores_git_sha_and_hash() -> None:
    # 40-символьный git SHA не должен считаться секретом.
    assert scan_text("commit = 356a192b7913b04c54574d18c28d46e6395428ab") == []


def test_shannon_entropy_monotonic() -> None:
    assert shannon_entropy("aaaa") < shannon_entropy("abcd")
    assert shannon_entropy("") == 0.0


def test_known_secretstore_value_detected() -> None:
    findings = scan_text(
        "config value = hunter2secretpassword",
        known_values=frozenset({"hunter2secretpassword"}),
    )
    assert any(f.rule == "secretstore-value" for f in findings)


def test_clean_text_no_findings() -> None:
    assert scan_text("def main():\n    return 42\n") == []


# --- denylist путей ---


def test_is_secret_path() -> None:
    assert is_secret_path(".env")
    assert is_secret_path("config/.env.production")
    assert is_secret_path("keys/server.pem")
    assert is_secret_path(".ssh/id_rsa")
    assert not is_secret_path("src/app.py")
    assert not is_secret_path(".env.example")  # шаблоны разрешены


def test_gitignore_block_covers_env() -> None:
    block = gitignore_block()
    assert ".env" in block
    assert "*.pem" in block


# --- гейт scan_files ---


def test_scan_files_flags_denylisted_path() -> None:
    findings = scan_files({".env": "FOO=bar"})
    assert any(f.rule == "secret-file-path" for f in findings)


def test_scan_files_flags_content() -> None:
    findings = scan_files({"deploy.sh": f"KEY={_FAKE_AWS}"})
    assert any(f.rule == "aws-access-key-id" for f in findings)


def test_scan_files_clean() -> None:
    assert scan_files({"src/app.py": "print('hello')\n"}) == []
