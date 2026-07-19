# Repository repair notes

This bundle fixes the repository setup without changing the application's core behavior.

Changes:

- added `requirements.txt` and `requirements-dev.txt`;
- replaced the malformed `.gitignore`;
- corrected source and PyInstaller commands to use `SnapSt.py`;
- configured `GITHUB_REPO` as `SevereClaw/SnapToGMod`;
- added a Windows GitHub Actions build and release workflow;
- added `.python-version` with Python 3.12;
- rewrote installation, build, release, and troubleshooting instructions.

The existing GitHub release should be replaced or followed by a release tagged `v1.0.0`. Future releases should use semantic-version tags such as `v1.0.1`.

No license file was added because the repository owner must choose the license explicitly.
