import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_post_form_has_csrf_field():
    for template in (ROOT / "templates").glob("*.html"):
        text = template.read_text(encoding="utf-8")
        post_forms = len(re.findall(r"<form\b(?=[^>]*\bmethod=[\"']post[\"'])[^>]*>", text, flags=re.I))
        csrf_fields = text.count('name="csrf_token"')
        assert csrf_fields == post_forms, f"{template.name}: {post_forms} POST forms but {csrf_fields} CSRF fields"


def test_runtime_templates_do_not_reference_barcode_cdns():
    templates = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "templates").glob("*.html"))
    assert "cdn.jsdelivr.net" not in templates
    assert "unpkg.com" not in templates
    assert "vendor/quagga.min.js" in templates


def test_public_repo_has_no_runtime_data_files():
    forbidden_names = {".env", ".thirtystash-secret"}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        assert path.name not in forbidden_names
        assert path.suffix not in {".db", ".sqlite", ".sqlite3"}


def test_public_defaults_are_hardened():
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "debug=True" not in app_py
    assert "change-me" not in compose
    assert "SECRET_KEY_FILE" in compose
    assert 'APP_VERSION = "1.2.0-beta.2"' in app_py

def test_food_scanner_stays_above_inventory():
    food = (ROOT / "templates" / "food.html").read_text(encoding="utf-8")
    assert food.index('id="add-food"') < food.index('<h2>Inventory</h2>')

