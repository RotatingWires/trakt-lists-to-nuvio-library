import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES_TO_SCAN = [
    ROOT / "README.md",
    ROOT / ".gitignore",
    ROOT / "trakt_lists_to_nuvio.py",
    ROOT / "tests" / "test_trakt_lists_to_nuvio.py",
]

FORBIDDEN_PATTERNS = {
    "Windows user profile path": re.compile(r"[A-Za-z]:\\Users\\[^\\\s\"']+", re.I),
    "Unix home path": re.compile(r"/home/[A-Za-z0-9._-]+"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    "OpenAI-style secret key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "JWT/access token": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


class RepositoryHygieneTests(unittest.TestCase):
    def test_no_obvious_pii_or_secrets_in_source_files(self):
        findings = []
        for path in FILES_TO_SCAN:
            text = path.read_text(encoding="utf-8")
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(text):
                    findings.append(f"{path.relative_to(ROOT)}: {label}")
        self.assertEqual([], findings, "Potential PII/secret material found: " + "; ".join(findings))

    def test_generated_personal_artifacts_are_gitignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for required in (
            "trakt-export*.zip",
            "*preview*.json",
            "*backup*.json",
            "*report*.json",
            ".env.*",
        ):
            self.assertIn(required, ignore)


if __name__ == "__main__":
    unittest.main()
