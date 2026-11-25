"""Owner commands on the server (run inside ~/sellingteen).

First time (and after every update, it never overwrites anything):
    python3.13 manage.py setup                    creates site.env, env/<app>.env (with fresh secret keys) and webhooks.json if missing
    python3.13 manage.py set-admin-password       turns on the Control Center at /admin (asks for a password twice)

Every day use:
    python3.13 manage.py accounts-all             every account in every app
    python3.13 manage.py reviewboost accounts
    python3.13 manage.py reviewboost activate their@email.com
    python3.13 manage.py reviewboost activate their@email.com --months 12
    python3.13 manage.py reviewboost deactivate their@email.com
    python3.13 manage.py backup                   makes a backup zip now (also done by nightly.py)
    python3.13 manage.py list                     shows the app names
"""
import getpass
import secrets
import shutil
import sys
from pathlib import Path

from sitehost import HERE, MANIFEST

GENERATE = "__GENERATE__"


def set_env_values(path, values):
    """Change (or add) KEY=value lines in a settings file, keeping every other line as it is."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.is_file() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            lines[i] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in done]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def fill_secrets(text):
    out = []
    for line in text.splitlines():
        if line.strip().endswith("=" + GENERATE):
            line = line.replace(GENERATE, secrets.token_hex(32))
        out.append(line)
    return "\n".join(out) + "\n"


def setup(root=HERE):
    root = Path(root)
    made = []
    if not (root / "site.env").exists() and (root / "site.env.example").exists():
        shutil.copy2(root / "site.env.example", root / "site.env")
        made.append("site.env")
    if not (root / "webhooks.json").exists() and (root / "webhooks.json.example").exists():
        shutil.copy2(root / "webhooks.json.example", root / "webhooks.json")
        made.append("webhooks.json")
    (root / "env").mkdir(exist_ok=True)
    for template in sorted((root / "env-templates").glob("*.env")):
        target = root / "env" / template.name
        if not target.exists():
            target.write_text(fill_secrets(template.read_text(encoding="utf-8")), encoding="utf-8")
            made.append(f"env/{template.name}")
    return made


def set_admin_password(root=HERE, password=None):
    from werkzeug.security import generate_password_hash
    if password is None:
        password = getpass.getpass("New Control Center password (at least 12 characters): ")
        again = getpass.getpass("Type it again: ")
        if password != again:
            sys.exit("The two passwords are different. Nothing changed.")
    if len(password) < 12:
        sys.exit("Use at least 12 characters. Nothing changed.")
    set_env_values(Path(root) / "site.env", {"ADMIN_PASSWORD_HASH": generate_password_hash(password),
                                             "ADMIN_SESSION_SECRET": secrets.token_hex(32)})
    return True


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help", "list"):
        print(__doc__)
        print("Apps:", ", ".join(MANIFEST["apps"]))
        return 0
    cmd = argv[1]
    if cmd == "setup":
        made = setup()
        print("Created: " + ", ".join(made) if made else "Everything was already set up. Nothing changed.")
        print("Next: open site.env and change YOURNAME, then Reload the web app.")
        return 0
    if cmd == "set-admin-password":
        set_admin_password()
        print("Saved. Reload the web app, then open /admin. (Old Control Center logins are logged out.)")
        return 0
    if cmd == "backup":
        from backup import make_backup
        path, n = make_backup()
        print(f"Backup saved: {path} ({n} files)")
        return 0
    if cmd == "accounts-all":
        from sitehost import Registry, build_app
        reg = Registry({slug: build_app(slug) for slug in MANIFEST["apps"]})
        for slug in reg.slugs:
            for a in reg.list_accounts(slug):
                print(f"{slug:15} {a['email']:40} {a['state']:6} {a.get('until') or '':10}  {a.get('business', '')}")
        return 0
    if cmd not in MANIFEST["apps"]:
        print(f"Unknown command or app '{cmd}'. Apps: {', '.join(MANIFEST['apps'])}")
        return 2
    from sitehost import build_app
    app = build_app(cmd)
    result = app.test_cli_runner().invoke(args=argv[2:])
    print(result.output, end="")
    if result.exception and not isinstance(result.exception, SystemExit):
        print(f"Error: {result.exception}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
