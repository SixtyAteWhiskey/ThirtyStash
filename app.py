import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from flask import Flask, Response, abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

APP_VERSION = "1.2.0-beta.2"


def load_or_create_secret_key():
    """Return an explicit secret or create a persistent per-installation secret file."""
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured

    secret_path = Path(os.getenv("SECRET_KEY_FILE", ".thirtystash-secret"))
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(secrets.token_urlsafe(48))
                handle.write("\n")
        except FileExistsError:
            # Another Gunicorn worker may have won the create race but not yet
            # flushed the new secret. Give that worker a brief chance to finish.
            pass

        value = ""
        for _ in range(20):
            try:
                value = secret_path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                value = ""
            if value:
                break
            time.sleep(0.05)
        if not value:
            raise RuntimeError("Secret key file is empty or could not be initialized.")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        return value
    except OSError as exc:
        raise RuntimeError(f"ThirtyStash could not create/read SECRET_KEY_FILE at {secret_path}: {exc}") from exc


app = Flask(__name__)
app.config["SECRET_KEY"] = load_or_create_secret_key()
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///thirtystash.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MiB restore upload limit

db = SQLAlchemy(app)

GRAMS_PER_OUNCE = 28.349523125
GRAMS_PER_POUND = 453.59237
LITERS_PER_GALLON = 3.785411784

ACTIVITY_MULTIPLIERS = {
    "sedentary": 1.2,
    "light": 1.375,
    "moderate": 1.55,
    "very": 1.725,
    "extra": 1.9,
}

TCCC_REFERENCE = [
    {"march": "M", "name": "CoTCCC-recommended limb tourniquet", "level": "core", "note": "For life-threatening extremity hemorrhage; training strongly recommended."},
    {"march": "M", "name": "Hemostatic gauze", "level": "core", "note": "For severe compressible bleeding when a tourniquet is not appropriate."},
    {"march": "M", "name": "Pressure dressing / compression bandage", "level": "core", "note": "Useful for wound packing and pressure dressing support."},
    {"march": "A", "name": "Basic airway adjunct / positioning supplies", "level": "trained", "note": "Select only for interventions you are trained and authorized to perform."},
    {"march": "R", "name": "Vented chest seal", "level": "core", "note": "TCCC guidance addresses chest seals for open chest wounds."},
    {"march": "R", "name": "Needle decompression equipment", "level": "advanced", "note": "Advanced/invasive intervention — trained and authorized personnel only."},
    {"march": "C", "name": "Trauma shears and marking pen", "level": "core", "note": "Supports exposure, reassessment, and documentation of interventions."},
    {"march": "H", "name": "Hypothermia prevention blanket / wrap", "level": "core", "note": "Preventing heat loss is a recurring TCCC priority."},
    {"march": "H", "name": "Rigid eye shield", "level": "trained", "note": "For suspected penetrating eye trauma; avoid pressure on the eye."},
]


class HouseholdMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    age = db.Column(db.Integer, nullable=True)
    weight_kg = db.Column(db.Float, nullable=True)
    height_cm = db.Column(db.Float, nullable=True)
    calc_profile = db.Column(db.String(20), nullable=True)  # male/female/manual
    activity_level = db.Column(db.String(20), nullable=False, default="sedentary")
    tdee_override = db.Column(db.Float, nullable=True)
    is_primary = db.Column(db.Boolean, nullable=False, default=False)

    @property
    def tdee(self):
        if self.tdee_override and self.tdee_override > 0:
            return round(self.tdee_override)
        if not all([self.age, self.weight_kg, self.height_cm]) or self.calc_profile not in {"male", "female"}:
            return None
        constant = 5 if self.calc_profile == "male" else -161
        bmr = (10 * self.weight_kg) + (6.25 * self.height_cm) - (5 * self.age) + constant
        return round(bmr * ACTIVITY_MULTIPLIERS.get(self.activity_level, 1.2))


class FoodItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    barcode = db.Column(db.String(64), nullable=True, index=True)
    name = db.Column(db.String(180), nullable=False)
    brand = db.Column(db.String(180), nullable=True)
    # Legacy v0.1 fields retained so existing prototype databases still open cleanly.
    quantity_units = db.Column(db.Float, nullable=False, default=1)
    unit_label = db.Column(db.String(40), nullable=False, default="g")
    calories_per_unit = db.Column(db.Float, nullable=False, default=0)
    # v0.2 food math: store canonical grams plus the nutrition-label serving values.
    total_grams = db.Column(db.Float, nullable=True)
    serving_size_g = db.Column(db.Float, nullable=True)
    calories_per_serving = db.Column(db.Float, nullable=True)
    expiry_date = db.Column(db.Date, nullable=True)
    last_inspected = db.Column(db.Date, nullable=True)
    inspection_interval_days = db.Column(db.Integer, nullable=False, default=75)
    image_url = db.Column(db.String(500), nullable=True)
    source = db.Column(db.String(40), nullable=False, default="manual")
    notes = db.Column(db.Text, nullable=True)
    lot_label = db.Column(db.String(120), nullable=True)
    purchase_date = db.Column(db.Date, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def total_calories(self):
        if self.total_grams and self.serving_size_g and self.calories_per_serving is not None and self.serving_size_g > 0:
            return round((self.total_grams / self.serving_size_g) * self.calories_per_serving)
        return round((self.quantity_units or 0) * (self.calories_per_unit or 0))

    @property
    def amount_grams(self):
        if self.total_grams is not None:
            return self.total_grams
        if self.unit_label == "g":
            return self.quantity_units or 0
        return None

    @property
    def next_inspection(self):
        base = self.last_inspected or self.created_at.date()
        return base + timedelta(days=self.inspection_interval_days)


class WaterItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    liters = db.Column(db.Float, nullable=False, default=0)
    treatment = db.Column(db.String(180), nullable=True)
    treatment_date = db.Column(db.Date, nullable=True)
    last_inspected = db.Column(db.Date, nullable=True)
    inspection_interval_days = db.Column(db.Integer, nullable=False, default=90)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def next_inspection(self):
        base = self.last_inspected or self.created_at.date()
        return base + timedelta(days=self.inspection_interval_days)


class MedicalItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(180), nullable=False)
    march_category = db.Column(db.String(20), nullable=False, default="Other")
    item_type = db.Column(db.String(30), nullable=False, default="intervention")
    quantity = db.Column(db.Float, nullable=False, default=1)
    expiry_date = db.Column(db.Date, nullable=True)
    last_inspected = db.Column(db.Date, nullable=True)
    inspection_interval_days = db.Column(db.Integer, nullable=False, default=90)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def next_inspection(self):
        base = self.last_inspected or self.created_at.date()
        return base + timedelta(days=self.inspection_interval_days)


class BackupSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True, default=1)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    backup_time = db.Column(db.String(5), nullable=False, default="02:00")
    destination_subdir = db.Column(db.String(255), nullable=False, default="daily")
    timezone_name = db.Column(db.String(80), nullable=False, default="UTC")
    next_backup_at = db.Column(db.DateTime, nullable=True)  # naive UTC
    last_backup_at = db.Column(db.DateTime, nullable=True)  # naive UTC
    last_backup_path = db.Column(db.String(500), nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def parse_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def safe_float(value, default=0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_text(value, max_length=None):
    value = (value or "").strip()
    if max_length is not None and len(value) > max_length:
        raise ValueError(f"Text is too long (maximum {max_length} characters).")
    return value


def strict_float(value, label, *, required=False, minimum=None, maximum=None):
    raw = (value or "").strip()
    if not raw:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        number = float(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a number.") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum:g}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be no more than {maximum:g}.")
    return number


def strict_int(value, label, *, required=False, minimum=None, maximum=None):
    raw = (value or "").strip()
    if not raw:
        if required:
            raise ValueError(f"{label} is required.")
        return None
    try:
        number = int(raw)
    except ValueError as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be no more than {maximum}.")
    return number


def validate_member_form(form):
    name = clean_text(form.get("name"), 120)
    if not name:
        raise ValueError("Name is required.")

    calc_profile = form.get("calc_profile") or "manual"
    if calc_profile not in {"male", "female", "manual"}:
        raise ValueError("Choose a valid calorie calculation profile.")
    activity = form.get("activity_level") or "sedentary"
    if activity not in ACTIVITY_MULTIPLIERS:
        raise ValueError("Choose a valid activity level.")

    age = strict_int(form.get("age"), "Age", minimum=1, maximum=120)
    weight = strict_float(form.get("weight") or form.get("weight_kg"), "Weight", minimum=0.01, maximum=2000)
    height = strict_float(form.get("height") or form.get("height_cm"), "Height", minimum=0.01, maximum=1000)
    override = strict_float(form.get("tdee_override"), "Daily calorie override", minimum=100, maximum=15000)

    weight_unit = form.get("weight_unit") or "kg"
    height_unit = form.get("height_unit") or "cm"
    if weight_unit not in {"kg", "lb"} or height_unit not in {"cm", "in"}:
        raise ValueError("Choose valid measurement units.")

    weight_kg = weight_to_kg(weight, weight_unit) if weight is not None else None
    height_cm = height_to_cm(height, height_unit) if height is not None else None
    if weight_kg is not None and not (5 <= weight_kg <= 500):
        raise ValueError("Weight looks outside the supported range. Check the value and unit.")
    if height_cm is not None and not (50 <= height_cm <= 250):
        raise ValueError("Height looks outside the supported range. Check the value and unit.")

    if calc_profile in {"male", "female"} and (age is None or weight_kg is None or height_cm is None):
        raise ValueError("Age, weight, and height are required for a calculated TDEE. Use Manual if you want to enter only a calorie target.")
    if calc_profile == "manual" and override is None:
        raise ValueError("Enter a daily calorie override when using the Manual calculation profile.")

    return {
        "name": name,
        "age": age,
        "weight_kg": weight_kg,
        "height_cm": height_cm,
        "calc_profile": calc_profile,
        "activity_level": activity,
        "tdee_override": override,
    }


def validate_inventory_date(value, label, *, future_ok=True):
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = parse_date(raw)
    if parsed is None:
        raise ValueError(f"{label} is not a valid date.")
    if not future_ok and parsed > date.today():
        raise ValueError(f"{label} cannot be in the future.")
    return parsed


def human_bytes(value):
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def weight_to_kg(value, unit):
    value = safe_float(value, 0)
    return value * 0.45359237 if unit == "lb" else value


def height_to_cm(value, unit):
    value = safe_float(value, 0)
    return value * 2.54 if unit == "in" else value


def mass_to_grams(value, unit):
    value = safe_float(value, 0)
    return {
        "g": value,
        "kg": value * 1000,
        "oz": value * GRAMS_PER_OUNCE,
        "lb": value * GRAMS_PER_POUND,
    }.get(unit, value)


def volume_to_liters(value, unit):
    value = safe_float(value, 0)
    return value * LITERS_PER_GALLON if unit == "gal" else value


def liters_to_gallons(value):
    return (value or 0) / LITERS_PER_GALLON


def get_backup_schedule():
    schedule = db.session.get(BackupSchedule, 1)
    if schedule is None:
        schedule = BackupSchedule(id=1)
        db.session.add(schedule)
        db.session.commit()
    return schedule


def normalize_backup_subdir(value):
    value = (value or "").strip().replace("\\", "/").strip("/")
    if not value or value == ".":
        return ""
    parts = [part for part in value.split("/") if part]
    if any(part in {".", ".."} for part in parts):
        raise ValueError("Backup folder must stay inside the configured backup root.")
    if any("\x00" in part for part in parts):
        raise ValueError("Backup folder contains an invalid character.")
    return "/".join(parts)


def backup_root():
    return os.path.realpath(os.getenv("BACKUP_ROOT", "/backups"))


def resolve_backup_destination(subdir):
    root = backup_root()
    clean = normalize_backup_subdir(subdir)
    candidate = os.path.realpath(os.path.join(root, clean))
    if os.path.commonpath([root, candidate]) != root:
        raise ValueError("Backup folder must stay inside the configured backup root.")
    return candidate


def validate_backup_destination(subdir):
    destination = resolve_backup_destination(subdir)
    probe = os.path.join(destination, ".thirtystash-write-test")
    try:
        os.makedirs(destination, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("ok")
        os.remove(probe)
    except OSError as exc:
        raise ValueError(f"Backup destination is not writable: {exc}") from exc
    return destination


def compute_next_backup_utc(time_text, timezone_name, now_utc=None):
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown timezone: {timezone_name}") from exc
    try:
        hour, minute = [int(part) for part in time_text.split(":", 1)]
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise ValueError("Backup time must be a valid 24-hour time.")

    aware_now_utc = now_utc or datetime.now(timezone.utc)
    if aware_now_utc.tzinfo is None:
        aware_now_utc = aware_now_utc.replace(tzinfo=timezone.utc)
    local_now = aware_now_utc.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def format_backup_datetime(value, timezone_name):
    if not value:
        return None
    try:
        tz = ZoneInfo(timezone_name or "UTC")
    except ZoneInfoNotFoundError:
        tz = timezone.utc
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return aware.astimezone(tz).strftime("%Y-%m-%d %I:%M %p")


def total_daily_calories():
    values = [m.tdee for m in HouseholdMember.query.all() if m.tdee]
    return sum(values)


def status_for(next_date, expiry_date=None):
    today = date.today()
    if expiry_date and expiry_date < today:
        return "expired"
    if next_date and next_date < today:
        return "due"
    if next_date and next_date <= today + timedelta(days=14):
        return "soon"
    if expiry_date and expiry_date <= today + timedelta(days=60):
        return "soon"
    return "ok"


def attention_items():
    """Return inventory items that need inspection or are near/past expiry."""
    today = date.today()
    rows = []

    def add(category, item, reason, due_date, severity, endpoint):
        rows.append({
            "category": category,
            "name": item.name,
            "reason": reason,
            "date": due_date,
            "severity": severity,
            "endpoint": endpoint,
            "id": item.id,
            "lot": getattr(item, "lot_label", None),
        })

    for item in FoodItem.query.all():
        if item.expiry_date:
            if item.expiry_date < today:
                add("Food", item, "Expired", item.expiry_date, "expired", "food")
            elif item.expiry_date <= today + timedelta(days=60):
                add("Food", item, "Expires soon", item.expiry_date, "soon", "food")
        if item.next_inspection < today:
            add("Food", item, "Inspection overdue", item.next_inspection, "due", "food")
        elif item.next_inspection <= today + timedelta(days=14):
            add("Food", item, "Inspection due soon", item.next_inspection, "soon", "food")

    for item in WaterItem.query.all():
        if item.next_inspection < today:
            add("Water", item, "Inspection overdue", item.next_inspection, "due", "water")
        elif item.next_inspection <= today + timedelta(days=14):
            add("Water", item, "Inspection due soon", item.next_inspection, "soon", "water")

    for item in MedicalItem.query.all():
        if item.expiry_date:
            if item.expiry_date < today:
                add("Medical", item, "Expired", item.expiry_date, "expired", "medical")
            elif item.expiry_date <= today + timedelta(days=60):
                add("Medical", item, "Expires soon", item.expiry_date, "soon", "medical")
        if item.next_inspection < today:
            add("Medical", item, "Inspection overdue", item.next_inspection, "due", "medical")
        elif item.next_inspection <= today + timedelta(days=14):
            add("Medical", item, "Inspection due soon", item.next_inspection, "soon", "medical")

    rank = {"expired": 0, "due": 1, "soon": 2}
    return sorted(rows, key=lambda r: (rank.get(r["severity"], 9), r["date"] or date.max, r["category"], r["name"]))


def inventory_matches_status(item, status):
    if not status or status == "all":
        return True
    current = status_for(item.next_inspection, getattr(item, "expiry_date", None))
    if status == "attention":
        return current in {"expired", "due", "soon"}
    if status == "overdue":
        return current == "due"
    if status == "expired":
        return current == "expired"
    if status == "soon":
        return current == "soon"
    if status == "ok":
        return current == "ok"
    return True


def text_matches(query, *values):
    if not query:
        return True
    haystack = " ".join(str(value or "") for value in values).lower()
    return query.lower() in haystack


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_helpers():
    return {
        "today": date.today(),
        "status_for": status_for,
        "liters_to_gallons": liters_to_gallons,
        "csrf_token": get_csrf_token,
        "app_version": APP_VERSION,
    }


@app.before_request
def csrf_protect():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    expected = session.get("_csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        abort(400, description="Invalid or missing CSRF token. Refresh the page and try again.")
    return None


@app.before_request
def require_onboarding():
    allowed = {"onboarding", "static", "healthz", "backup_restore"}
    if request.endpoint and request.endpoint not in allowed and HouseholdMember.query.count() == 0:
        return redirect(url_for("onboarding"))


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(self)")
    return response


def csv_safe(value):
    """Render values safely for spreadsheet applications that interpret formulas."""
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text_value = str(value)
    if text_value.startswith(("=", "+", "-", "@")):
        return "'" + text_value
    return text_value


def inventory_export_rows(section="all"):
    rows = []

    if section in {"all", "food"}:
        for item in FoodItem.query.order_by(FoodItem.name).all():
            rows.append({
                "Category": "Food",
                "Item": item.name,
                "Brand / Type": item.brand or "",
                "Quantity": round(item.amount_grams or 0, 2),
                "Unit": "g",
                "Calories": item.total_calories,
                "Serving Size (g)": round(item.serving_size_g, 2) if item.serving_size_g is not None else "",
                "Calories / Serving": round(item.calories_per_serving, 2) if item.calories_per_serving is not None else "",
                "Barcode": item.barcode or "",
                "Lot / Batch": item.lot_label or "",
                "Purchased / Stored Date": item.purchase_date,
                "Treatment / MARCH": "",
                "Treatment Date": "",
                "Expiry Date": item.expiry_date,
                "Last Inspected": item.last_inspected,
                "Next Inspection": item.next_inspection,
                "Inspection Interval (days)": item.inspection_interval_days,
                "Source": item.source or "",
                "Notes": item.notes or "",
            })

    if section in {"all", "water"}:
        for item in WaterItem.query.order_by(WaterItem.name).all():
            rows.append({
                "Category": "Water",
                "Item": item.name,
                "Brand / Type": "",
                "Quantity": round(item.liters, 2),
                "Unit": "L",
                "Calories": "",
                "Serving Size (g)": "",
                "Calories / Serving": "",
                "Barcode": "",
                "Lot / Batch": "",
                "Purchased / Stored Date": "",
                "Treatment / MARCH": item.treatment or "",
                "Treatment Date": item.treatment_date,
                "Expiry Date": "",
                "Last Inspected": item.last_inspected,
                "Next Inspection": item.next_inspection,
                "Inspection Interval (days)": item.inspection_interval_days,
                "Source": "",
                "Notes": item.notes or "",
            })

    if section in {"all", "medical"}:
        for item in MedicalItem.query.order_by(MedicalItem.march_category, MedicalItem.name).all():
            rows.append({
                "Category": "Medical",
                "Item": item.name,
                "Brand / Type": item.item_type,
                "Quantity": round(item.quantity, 2),
                "Unit": "count",
                "Calories": "",
                "Serving Size (g)": "",
                "Calories / Serving": "",
                "Barcode": "",
                "Lot / Batch": "",
                "Purchased / Stored Date": "",
                "Treatment / MARCH": item.march_category,
                "Treatment Date": "",
                "Expiry Date": item.expiry_date,
                "Last Inspected": item.last_inspected,
                "Next Inspection": item.next_inspection,
                "Inspection Interval (days)": item.inspection_interval_days,
                "Source": "",
                "Notes": item.notes or "",
            })

    return rows


@app.get("/export/inventory.csv")
def export_inventory_csv():
    section = (request.args.get("section") or "all").lower()
    if section not in {"all", "food", "water", "medical"}:
        section = "all"

    fieldnames = [
        "Category", "Item", "Brand / Type", "Quantity", "Unit", "Calories",
        "Serving Size (g)", "Calories / Serving", "Barcode", "Lot / Batch",
        "Purchased / Stored Date", "Treatment / MARCH", "Treatment Date", "Expiry Date", "Last Inspected", "Next Inspection",
        "Inspection Interval (days)", "Source", "Notes",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in inventory_export_rows(section):
        writer.writerow({key: csv_safe(row.get(key, "")) for key in fieldnames})

    # UTF-8 BOM helps Excel detect encoding correctly while remaining valid CSV.
    body = "\ufeff" + output.getvalue()
    suffix = "inventory" if section == "all" else section
    filename = f"ThirtyStash-{suffix}-{date.today().isoformat()}.csv"
    return Response(
        body,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )



def create_sqlite_backup_archive():
    """Create a consistent, integrity-checked snapshot of the complete SQLite database."""
    database_path = db.engine.url.database
    if not database_path:
        raise RuntimeError("ThirtyStash could not determine the SQLite database path.")

    database_path = os.path.abspath(database_path)
    if not os.path.exists(database_path):
        raise RuntimeError(f"SQLite database was not found at {database_path}.")

    with tempfile.TemporaryDirectory(prefix="thirtystash-backup-") as tmpdir:
        snapshot_path = os.path.join(tmpdir, "thirtystash.db")

        # sqlite3.Connection.backup() takes a transactionally consistent snapshot,
        # even while the application is running and serving normal reads/writes.
        source = sqlite3.connect(database_path, timeout=30)
        destination = sqlite3.connect(snapshot_path)
        try:
            source.backup(destination)
            integrity = destination.execute("PRAGMA integrity_check").fetchone()
            if not integrity or integrity[0] != "ok":
                result = integrity[0] if integrity else "no result"
                raise RuntimeError(f"Backup integrity check failed: {result}")
            snapshot_counts = {
                "household_members": destination.execute("SELECT COUNT(*) FROM household_member").fetchone()[0],
                "food_items": destination.execute("SELECT COUNT(*) FROM food_item").fetchone()[0],
                "water_items": destination.execute("SELECT COUNT(*) FROM water_item").fetchone()[0],
                "medical_items": destination.execute("SELECT COUNT(*) FROM medical_item").fetchone()[0],
            }
        finally:
            destination.close()
            source.close()

        created = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        manifest = {
            "application": "ThirtyStash",
            "version": APP_VERSION,
            "created_utc": created,
            "database_file": "thirtystash.db",
            "integrity_check": "ok",
            "counts": snapshot_counts,
        }

        restore_text = f"""ThirtyStash backup created {created}

This archive contains a complete SQLite snapshot of ThirtyStash.
The file thirtystash.db contains household data and all Food, Water,
and Medical inventory records.

RESTORE
-------
Use ThirtyStash's Restore backup screen and upload this ZIP. ThirtyStash will
validate the archive, run an SQLite integrity check, create a pre-restore safety
backup of the current database, and then restore the snapshot.

Manual recovery remains possible by extracting thirtystash.db and replacing the
container database while both ThirtyStash services are stopped.
"""

        archive = io.BytesIO()
        with zipfile.ZipFile(archive, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot_path, arcname="thirtystash.db")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
            zf.writestr("RESTORE.txt", restore_text)
        archive.seek(0)
        return archive


def write_backup_archive(destination_subdir="", filename_prefix="ThirtyStash-backup"):
    """Write a full backup atomically beneath BACKUP_ROOT and return its final path."""
    destination = validate_backup_destination(destination_subdir)
    archive = create_sqlite_backup_archive()
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", filename_prefix).strip("-") or "ThirtyStash-backup"
    filename = f"{safe_prefix}-{timestamp}.zip"
    final_path = os.path.join(destination, filename)
    temp_path = final_path + ".tmp"
    try:
        with open(temp_path, "wb") as handle:
            handle.write(archive.getbuffer())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, final_path)
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    return final_path


@app.get("/backup/full.zip")
def backup_full_zip():
    try:
        archive = create_sqlite_backup_archive()
    except Exception as exc:
        app.logger.exception("ThirtyStash backup failed")
        flash(f"Backup failed: {exc}", "error")
        return redirect(url_for("dashboard"))

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"ThirtyStash-backup-{timestamp}.zip",
        max_age=0,
    )


@app.post("/backup/now")
def backup_now():
    """Immediately save a full backup to the configured backup folder."""
    schedule = get_backup_schedule()
    try:
        path = write_backup_archive(schedule.destination_subdir)
        completed = datetime.now(timezone.utc).replace(tzinfo=None)
        schedule.last_backup_at = completed
        schedule.last_backup_path = path
        schedule.last_error = None
        db.session.commit()
        flash(f"Backup saved: {os.path.basename(path)}", "success")
    except Exception as exc:
        db.session.rollback()
        schedule = get_backup_schedule()
        schedule.last_error = str(exc)
        db.session.commit()
        app.logger.exception("Immediate ThirtyStash backup failed")
        flash(f"Backup failed: {exc}", "error")
    return redirect(url_for("settings") + "#backup-schedule")



def validate_restore_archive(uploaded_file):
    """Validate an uploaded ThirtyStash ZIP and return a temporary SQLite snapshot path + metadata."""
    if not uploaded_file or not uploaded_file.filename:
        raise ValueError("Choose a ThirtyStash backup ZIP first.")
    if not uploaded_file.filename.lower().endswith(".zip"):
        raise ValueError("ThirtyStash restore expects a .zip backup created by ThirtyStash.")

    upload_tmp = tempfile.NamedTemporaryFile(prefix="thirtystash-upload-", suffix=".zip", delete=False)
    upload_path = upload_tmp.name
    snapshot_path = None
    try:
        uploaded_file.save(upload_tmp)
        upload_tmp.close()
        if not zipfile.is_zipfile(upload_path):
            raise ValueError("The uploaded file is not a valid ZIP archive.")

        with zipfile.ZipFile(upload_path, "r") as archive:
            names = set(archive.namelist())
            if "thirtystash.db" not in names:
                raise ValueError("This ZIP does not contain thirtystash.db.")
            db_info = archive.getinfo("thirtystash.db")
            if db_info.file_size > 500 * 1024 * 1024:
                raise ValueError("The database inside this backup is unexpectedly large.")

            manifest = {}
            if "manifest.json" in names:
                try:
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ValueError("The backup manifest is invalid.") from exc
                if manifest.get("application") not in {None, "ThirtyStash"}:
                    raise ValueError("This backup does not identify itself as a ThirtyStash backup.")

            snapshot_file = tempfile.NamedTemporaryFile(prefix="thirtystash-restore-", suffix=".db", delete=False)
            snapshot_path = snapshot_file.name
            with archive.open("thirtystash.db", "r") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    snapshot_file.write(chunk)
            snapshot_file.close()

        source_db = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True, timeout=10)
        try:
            result = source_db.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ValueError(f"Backup database integrity check failed: {result[0] if result else 'no result'}")
            tables = {row[0] for row in source_db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            required_tables = {"household_member", "food_item", "water_item", "medical_item"}
            missing = sorted(required_tables - tables)
            if missing:
                raise ValueError("Backup is missing required ThirtyStash tables: " + ", ".join(missing))
            counts = {}
            for table in sorted(required_tables):
                counts[table] = source_db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            source_db.close()

        metadata = {
            "filename": uploaded_file.filename,
            "manifest": manifest,
            "counts": counts,
        }
        return snapshot_path, metadata
    except Exception:
        if snapshot_path and os.path.exists(snapshot_path):
            os.remove(snapshot_path)
        raise
    finally:
        if not upload_tmp.closed:
            upload_tmp.close()
        if os.path.exists(upload_path):
            os.remove(upload_path)


def extract_snapshot_from_backup_path(archive_path):
    """Extract thirtystash.db from a locally-created backup ZIP into a temporary file."""
    snapshot_file = tempfile.NamedTemporaryFile(prefix="thirtystash-safety-", suffix=".db", delete=False)
    snapshot_path = snapshot_file.name
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            with archive.open("thirtystash.db", "r") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    snapshot_file.write(chunk)
        snapshot_file.close()
        return snapshot_path
    except Exception:
        if not snapshot_file.closed:
            snapshot_file.close()
        if os.path.exists(snapshot_path):
            os.remove(snapshot_path)
        raise


def restore_sqlite_snapshot(snapshot_path):
    """Restore a validated SQLite snapshot into the live database using SQLite's backup API."""
    database_path = db.engine.url.database
    if not database_path:
        raise RuntimeError("ThirtyStash could not determine the SQLite database path.")
    database_path = os.path.abspath(database_path)

    # Release this web process's pooled handles before copying pages into the live DB.
    db.session.remove()
    db.engine.dispose()

    source = sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True, timeout=30)
    destination = sqlite3.connect(database_path, timeout=30)
    try:
        source.backup(destination)
        destination.commit()
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError(f"Restored database integrity check failed: {result[0] if result else 'no result'}")
    finally:
        destination.close()
        source.close()

    db.engine.dispose()
    ensure_prototype_schema()


@app.route("/backup/restore", methods=["GET", "POST"])
def backup_restore():
    if request.method == "POST":
        if request.form.get("confirm_restore") != "yes" or (request.form.get("restore_phrase") or "").strip().upper() != "RESTORE":
            flash('To restore, check the confirmation box and type RESTORE.', "error")
            return redirect(url_for("backup_restore"))

        snapshot_path = None
        safety_path = None
        restore_started = False
        rollback_snapshot = None
        try:
            snapshot_path, metadata = validate_restore_archive(request.files.get("backup_file"))
            current_schedule = get_backup_schedule() if HouseholdMember.query.count() else None
            safety_subdir = current_schedule.destination_subdir if current_schedule else ""
            safety_path = write_backup_archive(safety_subdir, filename_prefix="ThirtyStash-pre-restore")
            restore_started = True
            restore_sqlite_snapshot(snapshot_path)
            session.clear()
            flash(
                f"Backup restored successfully. Pre-restore safety copy: {os.path.basename(safety_path)}",
                "success",
            )
            return redirect(url_for("dashboard"))
        except Exception as exc:
            rollback_note = ""
            if restore_started and safety_path:
                try:
                    rollback_snapshot = extract_snapshot_from_backup_path(safety_path)
                    restore_sqlite_snapshot(rollback_snapshot)
                    rollback_note = " The previous live database was automatically restored from the pre-restore safety backup."
                except Exception:
                    app.logger.exception("Automatic rollback after failed restore also failed")
                    rollback_note = " Automatic rollback also failed; use the pre-restore safety backup for manual recovery."
            app.logger.exception("ThirtyStash restore failed")
            flash(f"Restore failed: {exc}.{rollback_note}", "error")
        finally:
            for temp_path in (snapshot_path, rollback_snapshot):
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
    return render_template("backup_restore.html")


@app.get("/reset")
def reset_start():
    session.pop("reset_token", None)
    return render_template("reset.html", stage=1)


@app.post("/reset/confirm-one")
def reset_confirm_one():
    if request.form.get("confirm_one") != "yes":
        flash("The first confirmation is required before continuing.", "error")
        return redirect(url_for("reset_start"))
    token = secrets.token_urlsafe(24)
    session["reset_token"] = token
    return render_template("reset.html", stage=2, reset_token=token)


@app.post("/reset/confirm-two")
def reset_confirm_two():
    expected = session.get("reset_token")
    supplied = request.form.get("reset_token")
    phrase = (request.form.get("reset_phrase") or "").strip().upper()
    if not expected or not secrets.compare_digest(expected, supplied or ""):
        flash("Reset confirmation expired. Start again.", "error")
        return redirect(url_for("reset_start"))
    if phrase != "RESET":
        flash('Second confirmation failed. Type RESET exactly to continue.', "error")
        return render_template("reset.html", stage=2, reset_token=expected)

    try:
        schedule = get_backup_schedule()
        safety_path = write_backup_archive(schedule.destination_subdir, filename_prefix="ThirtyStash-pre-reset")
        db.session.query(FoodItem).delete(synchronize_session=False)
        db.session.query(WaterItem).delete(synchronize_session=False)
        db.session.query(MedicalItem).delete(synchronize_session=False)
        db.session.query(HouseholdMember).delete(synchronize_session=False)
        db.session.query(BackupSchedule).delete(synchronize_session=False)
        db.session.commit()
        session.clear()
        flash(f"ThirtyStash was reset. Safety backup saved as {os.path.basename(safety_path)}.", "success")
        return redirect(url_for("onboarding"))
    except Exception as exc:
        db.session.rollback()
        app.logger.exception("ThirtyStash reset failed")
        flash(f"Reset was not performed: {exc}", "error")
        return redirect(url_for("reset_start"))


@app.post("/backup/schedule")
def backup_schedule_update():
    schedule = get_backup_schedule()
    enabled = request.form.get("enabled") == "on"
    backup_time = (request.form.get("backup_time") or "02:00").strip()
    destination_subdir = request.form.get("destination_subdir") or ""
    timezone_name = (request.form.get("timezone_name") or schedule.timezone_name or "UTC").strip()

    try:
        destination_subdir = normalize_backup_subdir(destination_subdir)
        validate_backup_destination(destination_subdir)
        next_backup_at = compute_next_backup_utc(backup_time, timezone_name) if enabled else None
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settings") + "#backup-schedule")

    schedule.enabled = enabled
    schedule.backup_time = backup_time
    schedule.destination_subdir = destination_subdir
    schedule.timezone_name = timezone_name
    schedule.next_backup_at = next_backup_at
    schedule.last_error = None
    db.session.commit()
    flash("Daily backup schedule updated." if enabled else "Scheduled backups disabled.", "success")
    return redirect(url_for("settings") + "#backup-schedule")


def database_file_path():
    database = db.engine.url.database
    if not database:
        return None
    return os.path.abspath(database)


def database_integrity_result():
    try:
        result = db.session.execute(text("PRAGMA integrity_check")).scalar()
        return str(result or "unknown")
    except Exception as exc:
        app.logger.exception("Database integrity check failed")
        return f"error: {exc}"


def collect_system_status():
    schedule = get_backup_schedule()
    db_path = database_file_path()
    integrity = database_integrity_result()
    db_size = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0

    counts = {
        "Household members": HouseholdMember.query.count(),
        "Food lots": FoodItem.query.count(),
        "Water entries": WaterItem.query.count(),
        "Medical items": MedicalItem.query.count(),
    }

    backup_destination = None
    backup_writable = False
    backup_error = None
    backup_usage = None
    recent_backups = []
    try:
        backup_destination = validate_backup_destination(schedule.destination_subdir)
        backup_writable = True
        usage = shutil.disk_usage(backup_destination)
        backup_usage = {
            "free": human_bytes(usage.free),
            "used": human_bytes(usage.used),
            "total": human_bytes(usage.total),
            "percent": round((usage.used / usage.total) * 100, 1) if usage.total else 0,
        }
        entries = []
        for name in os.listdir(backup_destination):
            if not name.lower().endswith(".zip"):
                continue
            path = os.path.join(backup_destination, name)
            if not os.path.isfile(path):
                continue
            entries.append((os.path.getmtime(path), name, os.path.getsize(path)))
        for modified, name, size in sorted(entries, reverse=True)[:5]:
            recent_backups.append({
                "name": name,
                "size": human_bytes(size),
                "modified": datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M"),
            })
    except Exception as exc:
        backup_error = str(exc)

    database_ok = integrity.lower() == "ok"
    overall = "healthy" if database_ok and backup_writable and not schedule.last_error else "warning"
    return {
        "version": APP_VERSION,
        "overall": overall,
        "database_ok": database_ok,
        "database_integrity": integrity,
        "database_path": db_path,
        "database_size": human_bytes(db_size),
        "counts": counts,
        "backup_root": backup_root(),
        "backup_destination": backup_destination,
        "backup_writable": backup_writable,
        "backup_error": backup_error,
        "backup_usage": backup_usage,
        "recent_backups": recent_backups,
        "schedule": schedule,
        "backup_last_local": format_backup_datetime(schedule.last_backup_at, schedule.timezone_name),
        "backup_next_local": format_backup_datetime(schedule.next_backup_at, schedule.timezone_name),
    }


@app.route("/healthz")
def healthz():
    try:
        db.session.execute(text("SELECT 1")).scalar()
        return {"status": "ok", "version": APP_VERSION, "database": "reachable"}
    except Exception as exc:
        app.logger.exception("Health check failed")
        return {"status": "error", "version": APP_VERSION, "database": "unreachable"}, 503


@app.get("/status")
def system_status():
    return render_template("status.html", **collect_system_status())


@app.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    if request.method == "POST":
        try:
            values = validate_member_form(request.form)
            member = HouseholdMember(**values, is_primary=True)
            db.session.add(member)
            db.session.commit()
            flash("Primary household member added. Add anyone else who should count toward your 30-day target.", "success")
            return redirect(url_for("household"))
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("onboarding.html", form=request.form)
    return render_template("onboarding.html", form={})


@app.route("/")
def dashboard():
    daily = total_daily_calories()
    stored_cals = sum(i.total_calories for i in FoodItem.query.all())
    food_days = stored_cals / daily if daily else 0
    people = HouseholdMember.query.count()
    water_target = people * 3 * 30
    stored_water = sum(i.liters for i in WaterItem.query.all())
    water_days = stored_water / (people * 3) if people else 0
    expiring_med = MedicalItem.query.filter(MedicalItem.expiry_date.isnot(None), MedicalItem.expiry_date <= date.today() + timedelta(days=60)).count()
    return render_template(
        "dashboard.html",
        daily=daily,
        stored_cals=stored_cals,
        food_days=food_days,
        calorie_target=daily * 30,
        people=people,
        water_target=water_target,
        stored_water=stored_water,
        water_days=water_days,
        expiring_med=expiring_med,
        attention=attention_items(),
    )


@app.get("/settings")
def settings():
    schedule = get_backup_schedule()
    return render_template(
        "settings.html",
        members=HouseholdMember.query.order_by(HouseholdMember.is_primary.desc(), HouseholdMember.name).all(),
        backup_schedule=schedule,
        backup_root=backup_root(),
        backup_host_path=os.getenv("BACKUP_HOST_PATH", "./backups"),
        backup_last_local=format_backup_datetime(schedule.last_backup_at, schedule.timezone_name),
        backup_next_local=format_backup_datetime(schedule.next_backup_at, schedule.timezone_name),
    )


@app.route("/household", methods=["GET", "POST"])
def household():
    if request.method == "POST":
        try:
            values = validate_member_form(request.form)
            member = HouseholdMember(**values)
            db.session.add(member)
            db.session.commit()
            flash(f"Added {member.name}.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(url_for("settings") + "#household")
    return redirect(url_for("settings") + "#household")


@app.post("/household/<int:item_id>/delete")
def household_delete(item_id):
    item = db.session.get(HouseholdMember, item_id)
    if item:
        if HouseholdMember.query.count() <= 1:
            flash("ThirtyStash needs at least one household member.", "error")
        else:
            db.session.delete(item)
            db.session.commit()
    return redirect(url_for("settings") + "#household")


@app.route("/food", methods=["GET", "POST"])
def food():
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), 180)
            if not name:
                raise ValueError("Food name is required.")
            brand = clean_text(request.form.get("brand"), 180) or None
            lot_label = clean_text(request.form.get("lot_label"), 120) or None
            notes = clean_text(request.form.get("notes"), 5000) or None
            barcode = re.sub(r"\s+", "", request.form.get("barcode") or "") or None
            if barcode and (len(barcode) > 64 or not barcode.isdigit()):
                raise ValueError("Barcode must contain digits only and be no more than 64 characters.")

            amount = strict_float(request.form.get("total_amount"), "Total food amount", required=True, minimum=0.01, maximum=5000000)
            serving = strict_float(request.form.get("serving_size"), "Serving size", required=True, minimum=0.01, maximum=100000)
            calories_per_serving = strict_float(request.form.get("calories_per_serving"), "Calories per serving", required=True, minimum=0, maximum=10000)
            total_unit = request.form.get("total_amount_unit") or "g"
            serving_unit = request.form.get("serving_size_unit") or "g"
            if total_unit not in {"g", "kg", "oz", "lb"} or serving_unit not in {"g", "oz", "lb"}:
                raise ValueError("Choose valid food measurement units.")
            total_grams = mass_to_grams(amount, total_unit)
            serving_size_g = mass_to_grams(serving, serving_unit)
            if total_grams > 5000000 or serving_size_g > 100000:
                raise ValueError("Food amount or serving size looks unusually large. Check the value and unit.")

            interval = strict_int(request.form.get("inspection_interval_days"), "Inspection interval", required=True)
            if interval not in {30, 60, 75, 90, 180}:
                raise ValueError("Choose a valid food inspection interval.")
            purchase_date = validate_inventory_date(request.form.get("purchase_date"), "Purchased / stored date", future_ok=False)
            expiry_date = validate_inventory_date(request.form.get("expiry_date"), "Expiry date")
            last_inspected = validate_inventory_date(request.form.get("last_inspected"), "Last inspected date", future_ok=False) or date.today()
            if purchase_date and expiry_date and expiry_date < purchase_date:
                raise ValueError("Expiry date cannot be earlier than the purchased / stored date.")

            calories_per_gram = calories_per_serving / serving_size_g
            item = FoodItem(
                barcode=barcode,
                name=name,
                brand=brand,
                quantity_units=total_grams,
                unit_label="g",
                calories_per_unit=calories_per_gram,
                total_grams=total_grams,
                serving_size_g=serving_size_g,
                calories_per_serving=calories_per_serving,
                expiry_date=expiry_date,
                last_inspected=last_inspected,
                inspection_interval_days=interval,
                image_url=clean_text(request.form.get("image_url"), 500) or None,
                source=clean_text(request.form.get("source"), 40) or "manual",
                notes=notes,
                lot_label=lot_label,
                purchase_date=purchase_date,
            )
            db.session.add(item)
            db.session.commit()
            flash("Food item added.", "success")
            return redirect(url_for("food"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("food") + "#add-food")

    all_items = FoodItem.query.all()
    daily = total_daily_calories()
    stored = sum(i.total_calories for i in all_items)
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").lower()
    sort = (request.args.get("sort") or "expiry").lower()
    items = [i for i in all_items if text_matches(q, i.name, i.brand, i.barcode, i.lot_label, i.notes, i.source) and inventory_matches_status(i, status)]
    if sort == "name":
        items.sort(key=lambda i: ((i.name or "").lower(), i.expiry_date or date.max))
    elif sort == "inspection":
        items.sort(key=lambda i: (i.next_inspection or date.max, (i.name or "").lower()))
    elif sort == "calories":
        items.sort(key=lambda i: (-i.total_calories, (i.name or "").lower()))
    elif sort == "newest":
        items.sort(key=lambda i: (i.created_at or datetime.min), reverse=True)
    else:
        sort = "expiry"
        items.sort(key=lambda i: (i.expiry_date or date.max, (i.name or "").lower()))
    copy_item = db.session.get(FoodItem, safe_int(request.args.get("copy"))) if request.args.get("copy") else None
    return render_template("food.html", items=items, total_items=len(all_items), q=q, status=status, sort=sort, daily=daily, stored=stored, target=daily * 30, coverage=(stored / daily if daily else 0), copy_item=copy_item)


@app.get("/api/food/barcode/<barcode>")
def food_barcode_existing(barcode):
    barcode = re.sub(r"[^0-9]", "", barcode)
    if not barcode:
        return jsonify({"ok": False, "count": 0, "items": []})
    matches = FoodItem.query.filter_by(barcode=barcode).order_by(FoodItem.expiry_date.asc().nullslast(), FoodItem.created_at.asc()).all()
    return jsonify({
        "ok": True,
        "count": len(matches),
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "lot": item.lot_label or "",
                "expiry": item.expiry_date.isoformat() if item.expiry_date else "",
                "grams": round(item.amount_grams or 0, 1),
                "add_lot_url": url_for("food", copy=item.id) + "#add-food",
            }
            for item in matches
        ],
    })


@app.get("/api/openfoodfacts/<barcode>")
def off_lookup(barcode):
    barcode = re.sub(r"[^0-9]", "", barcode)
    if not barcode:
        return jsonify({"ok": False, "error": "Invalid barcode"}), 400
    fields = "code,product_name,brands,quantity,serving_size,serving_quantity,serving_quantity_unit,nutriments,image_front_small_url"
    url = f"https://world.openfoodfacts.org/api/v3/product/{barcode}.json"
    headers = {"User-Agent": os.getenv("OFF_USER_AGENT", "ThirtyStash/1.2.0-beta.2 (self-hosted app)")}
    try:
        response = requests.get(url, params={"fields": fields}, headers=headers, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        return jsonify({"ok": False, "error": f"Open Food Facts lookup failed: {exc}"}), 502

    product = payload.get("product") or {}
    if not product:
        return jsonify({"ok": False, "error": "Product not found. You can still enter it manually."}), 404
    nutriments = product.get("nutriments") or {}
    kcal_100g = nutriments.get("energy-kcal_100g")
    kcal_serving = nutriments.get("energy-kcal_serving")
    return jsonify({
        "ok": True,
        "barcode": barcode,
        "name": product.get("product_name") or "",
        "brand": product.get("brands") or "",
        "quantity": product.get("quantity") or "",
        "serving_size": product.get("serving_size") or "",
        "serving_quantity": product.get("serving_quantity") or "",
        "serving_quantity_unit": product.get("serving_quantity_unit") or "",
        "calories_100g": kcal_100g,
        "calories_serving": kcal_serving,
        "image_url": product.get("image_front_small_url") or "",
        "source": "Open Food Facts",
    })


@app.route("/food/<int:item_id>/edit", methods=["GET", "POST"])
def food_edit(item_id):
    item = db.session.get(FoodItem, item_id)
    if not item:
        flash("Food item not found.", "error")
        return redirect(url_for("food"))
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), 180)
            if not name:
                raise ValueError("Food name is required.")
            barcode = re.sub(r"\s+", "", request.form.get("barcode") or "") or None
            if barcode and (len(barcode) > 64 or not barcode.isdigit()):
                raise ValueError("Barcode must contain digits only and be no more than 64 characters.")
            amount = strict_float(request.form.get("total_amount"), "Total food amount", required=True, minimum=0.01, maximum=5000000)
            serving = strict_float(request.form.get("serving_size"), "Serving size", required=True, minimum=0.01, maximum=100000)
            calories = strict_float(request.form.get("calories_per_serving"), "Calories per serving", required=True, minimum=0, maximum=10000)
            total_unit = request.form.get("total_amount_unit") or "g"
            serving_unit = request.form.get("serving_size_unit") or "g"
            if total_unit not in {"g", "kg", "oz", "lb"} or serving_unit not in {"g", "oz", "lb"}:
                raise ValueError("Choose valid food measurement units.")
            total_grams = mass_to_grams(amount, total_unit)
            serving_size_g = mass_to_grams(serving, serving_unit)
            interval = strict_int(request.form.get("inspection_interval_days"), "Inspection interval", required=True)
            if interval not in {30, 60, 75, 90, 180}:
                raise ValueError("Choose a valid food inspection interval.")
            purchase = validate_inventory_date(request.form.get("purchase_date"), "Purchased / stored date", future_ok=False)
            expiry = validate_inventory_date(request.form.get("expiry_date"), "Expiry date")
            inspected = validate_inventory_date(request.form.get("last_inspected"), "Last inspected date", future_ok=False) or item.last_inspected
            if purchase and expiry and expiry < purchase:
                raise ValueError("Expiry date cannot be earlier than the purchased / stored date.")

            item.barcode = barcode
            item.name = name
            item.brand = clean_text(request.form.get("brand"), 180) or None
            item.quantity_units = total_grams
            item.unit_label = "g"
            item.calories_per_unit = calories / serving_size_g
            item.total_grams = total_grams
            item.serving_size_g = serving_size_g
            item.calories_per_serving = calories
            item.expiry_date = expiry
            item.last_inspected = inspected
            item.inspection_interval_days = interval
            item.lot_label = clean_text(request.form.get("lot_label"), 120) or None
            item.purchase_date = purchase
            item.notes = clean_text(request.form.get("notes"), 5000) or None
            db.session.commit()
            flash(f"Updated {item.name}.", "success")
            return redirect(url_for("food"))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("food_edit.html", item=item)


@app.post("/food/<int:item_id>/inspect")
def food_inspect(item_id):
    item = db.session.get(FoodItem, item_id)
    if item:
        item.last_inspected = date.today()
        db.session.commit()
        flash(f"Marked {item.name} inspected today.", "success")
    return redirect(url_for("food"))


@app.post("/food/<int:item_id>/delete")
def food_delete(item_id):
    item = db.session.get(FoodItem, item_id)
    if item:
        db.session.delete(item)
        db.session.commit()
    return redirect(url_for("food"))


@app.route("/water", methods=["GET", "POST"])
def water():
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), 180)
            if not name:
                raise ValueError("Water entry needs a container or location name.")
            amount = strict_float(request.form.get("amount") or request.form.get("liters"), "Water amount", required=True, minimum=0.01, maximum=1000000)
            unit = request.form.get("water_unit") or "L"
            if unit not in {"L", "gal"}:
                raise ValueError("Choose a valid water unit.")
            liters = volume_to_liters(amount, unit)
            if liters > 1000000:
                raise ValueError("Water amount looks unusually large. Check the value and unit.")
            interval = strict_int(request.form.get("inspection_interval_days"), "Inspection interval", required=True)
            if interval not in {30, 60, 90, 180, 365}:
                raise ValueError("Choose a valid water inspection interval.")
            treatment_date = validate_inventory_date(request.form.get("treatment_date"), "Treatment date", future_ok=False)
            last_inspected = validate_inventory_date(request.form.get("last_inspected"), "Last inspected date", future_ok=False) or date.today()
            item = WaterItem(
                name=name,
                liters=liters,
                treatment=clean_text(request.form.get("treatment"), 180) or None,
                treatment_date=treatment_date,
                last_inspected=last_inspected,
                inspection_interval_days=interval,
                notes=clean_text(request.form.get("notes"), 5000) or None,
            )
            db.session.add(item)
            db.session.commit()
            flash("Water storage added.", "success")
            return redirect(url_for("water"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("water"))
    all_items = WaterItem.query.all()
    people = HouseholdMember.query.count()
    target = people * 3 * 30
    stored = sum(i.liters for i in all_items)
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").lower()
    sort = (request.args.get("sort") or "name").lower()
    items = [i for i in all_items if text_matches(q, i.name, i.treatment, i.notes) and inventory_matches_status(i, status)]
    if sort == "inspection":
        items.sort(key=lambda i: (i.next_inspection or date.max, (i.name or "").lower()))
    elif sort == "amount":
        items.sort(key=lambda i: (-i.liters, (i.name or "").lower()))
    elif sort == "newest":
        items.sort(key=lambda i: (i.created_at or datetime.min), reverse=True)
    else:
        sort = "name"
        items.sort(key=lambda i: (i.name or "").lower())
    return render_template("water.html", items=items, total_items=len(all_items), q=q, status=status, sort=sort, people=people, target=target, stored=stored, coverage=(stored / (people * 3) if people else 0))


@app.route("/water/<int:item_id>/edit", methods=["GET", "POST"])
def water_edit(item_id):
    item = db.session.get(WaterItem, item_id)
    if not item:
        flash("Water item not found.", "error")
        return redirect(url_for("water"))
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), 180)
            if not name:
                raise ValueError("Water entry needs a container or location name.")
            amount = strict_float(request.form.get("amount"), "Water amount", required=True, minimum=0.01, maximum=1000000)
            unit = request.form.get("water_unit") or "L"
            if unit not in {"L", "gal"}:
                raise ValueError("Choose a valid water unit.")
            liters = volume_to_liters(amount, unit)
            interval = strict_int(request.form.get("inspection_interval_days"), "Inspection interval", required=True)
            if interval not in {30, 60, 90, 180, 365}:
                raise ValueError("Choose a valid water inspection interval.")
            item.name = name
            item.liters = liters
            item.treatment = clean_text(request.form.get("treatment"), 180) or None
            item.treatment_date = validate_inventory_date(request.form.get("treatment_date"), "Treatment date", future_ok=False)
            item.last_inspected = validate_inventory_date(request.form.get("last_inspected"), "Last inspected date", future_ok=False) or item.last_inspected
            item.inspection_interval_days = interval
            item.notes = clean_text(request.form.get("notes"), 5000) or None
            db.session.commit()
            flash(f"Updated {item.name}.", "success")
            return redirect(url_for("water"))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("water_edit.html", item=item)


@app.post("/water/<int:item_id>/inspect")
def water_inspect(item_id):
    item = db.session.get(WaterItem, item_id)
    if item:
        item.last_inspected = date.today()
        db.session.commit()
    return redirect(url_for("water"))


@app.post("/water/<int:item_id>/delete")
def water_delete(item_id):
    item = db.session.get(WaterItem, item_id)
    if item:
        db.session.delete(item)
        db.session.commit()
    return redirect(url_for("water"))


@app.route("/medical", methods=["GET", "POST"])
def medical():
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), 180)
            if not name:
                raise ValueError("Medical item name is required.")
            category = request.form.get("march_category") or "Other"
            item_type = request.form.get("item_type") or "intervention"
            if category not in {"M", "A", "R", "C", "H", "Medication", "Other"}:
                raise ValueError("Choose a valid MARCH/category value.")
            if item_type not in {"intervention", "otc", "prescription", "equipment"}:
                raise ValueError("Choose a valid medical item type.")
            quantity = strict_float(request.form.get("quantity"), "Quantity", required=True, minimum=0.01, maximum=1000000)
            interval = strict_int(request.form.get("inspection_interval_days"), "Inspection interval", required=True)
            if interval not in {30, 60, 90, 180, 365}:
                raise ValueError("Choose a valid medical inspection interval.")
            item = MedicalItem(
                name=name,
                march_category=category,
                item_type=item_type,
                quantity=quantity,
                expiry_date=validate_inventory_date(request.form.get("expiry_date"), "Expiry date"),
                last_inspected=validate_inventory_date(request.form.get("last_inspected"), "Last inspected date", future_ok=False) or date.today(),
                inspection_interval_days=interval,
                notes=clean_text(request.form.get("notes"), 5000) or None,
            )
            db.session.add(item)
            db.session.commit()
            flash("Medical item added.", "success")
            return redirect(url_for("medical"))
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("medical"))
    all_items = MedicalItem.query.all()
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").lower()
    sort = (request.args.get("sort") or "category").lower()
    category = (request.args.get("category") or "all")
    items = [i for i in all_items if text_matches(q, i.name, i.item_type, i.march_category, i.notes) and inventory_matches_status(i, status)]
    if category != "all":
        items = [i for i in items if i.march_category == category]
    if sort == "expiry":
        items.sort(key=lambda i: (i.expiry_date or date.max, (i.name or "").lower()))
    elif sort == "inspection":
        items.sort(key=lambda i: (i.next_inspection or date.max, (i.name or "").lower()))
    elif sort == "name":
        items.sort(key=lambda i: (i.name or "").lower())
    elif sort == "newest":
        items.sort(key=lambda i: (i.created_at or datetime.min), reverse=True)
    else:
        sort = "category"
        items.sort(key=lambda i: (i.march_category or "", (i.name or "").lower()))
    return render_template("medical.html", items=items, total_items=len(all_items), q=q, status=status, sort=sort, category=category, recommendations=TCCC_REFERENCE)


@app.route("/medical/<int:item_id>/edit", methods=["GET", "POST"])
def medical_edit(item_id):
    item = db.session.get(MedicalItem, item_id)
    if not item:
        flash("Medical item not found.", "error")
        return redirect(url_for("medical"))
    if request.method == "POST":
        try:
            name = clean_text(request.form.get("name"), 180)
            if not name:
                raise ValueError("Medical item name is required.")
            category = request.form.get("march_category") or "Other"
            item_type = request.form.get("item_type") or "intervention"
            if category not in {"M", "A", "R", "C", "H", "Medication", "Other"}:
                raise ValueError("Choose a valid MARCH/category value.")
            if item_type not in {"intervention", "otc", "prescription", "equipment"}:
                raise ValueError("Choose a valid medical item type.")
            quantity = strict_float(request.form.get("quantity"), "Quantity", required=True, minimum=0.01, maximum=1000000)
            interval = strict_int(request.form.get("inspection_interval_days"), "Inspection interval", required=True)
            if interval not in {30, 60, 90, 180, 365}:
                raise ValueError("Choose a valid medical inspection interval.")
            item.name = name
            item.march_category = category
            item.item_type = item_type
            item.quantity = quantity
            item.expiry_date = validate_inventory_date(request.form.get("expiry_date"), "Expiry date")
            item.last_inspected = validate_inventory_date(request.form.get("last_inspected"), "Last inspected date", future_ok=False) or item.last_inspected
            item.inspection_interval_days = interval
            item.notes = clean_text(request.form.get("notes"), 5000) or None
            db.session.commit()
            flash(f"Updated {item.name}.", "success")
            return redirect(url_for("medical"))
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("medical_edit.html", item=item)


@app.post("/medical/<int:item_id>/inspect")
def medical_inspect(item_id):
    item = db.session.get(MedicalItem, item_id)
    if item:
        item.last_inspected = date.today()
        db.session.commit()
    return redirect(url_for("medical"))


@app.post("/medical/<int:item_id>/delete")
def medical_delete(item_id):
    item = db.session.get(MedicalItem, item_id)
    if item:
        db.session.delete(item)
        db.session.commit()
    return redirect(url_for("medical"))


def ensure_prototype_schema():
    """Keep early prototype databases usable without introducing Alembic yet."""
    db.create_all()
    inspector = inspect(db.engine)
    columns = {c["name"] for c in inspector.get_columns("food_item")}
    additions = {
        "total_grams": "FLOAT",
        "serving_size_g": "FLOAT",
        "calories_per_serving": "FLOAT",
        "lot_label": "VARCHAR(120)",
        "purchase_date": "DATE",
    }
    for column, sql_type in additions.items():
        if column not in columns:
            with db.engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE food_item ADD COLUMN {column} {sql_type}"))


with app.app_context():
    ensure_prototype_schema()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3055, debug=False)
