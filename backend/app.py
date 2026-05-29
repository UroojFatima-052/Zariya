from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_mail import Mail, Message
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from config import Config
from extensions import db
from models import User, Donation, NGO, NGOApplication, NGONeed, DonorAlert, DonationDraft, Complaint, PasswordResetToken
from datetime import datetime, timedelta
from collections import Counter, defaultdict
import re
import os
import time
import secrets


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app():
    app = Flask(
        __name__,
        static_folder=os.path.join(BASE_DIR, "frontend", "static"),
        template_folder=os.path.join(BASE_DIR, "frontend", "templates")
    )
    app.config.from_object(Config)
    app.config["SERVER_INSTANCE_ID"] = secrets.token_hex(16)
    SESSION_TIMEOUT_SECONDS = 10 * 60  # 10 minutes

    db.init_app(app)
    mail = Mail(app)
    limiter = Limiter(key_func=get_remote_address, app=app, default_limits=[])

    with app.app_context():
        db.create_all()
        _migrate_db(db)
        seed_ngos_if_empty()
        seed_default_admin()

    @app.before_request
    def enforce_session_security():
        user_id = session.get("user_id")
        if not user_id:
            return

        if session.get("server_instance_id") != app.config["SERVER_INSTANCE_ID"]:
            session.clear()
            return 

        # Inactivity timeout
        now = int(time.time())
        last_seen = session.get("last_seen", now)

        if now - last_seen > SESSION_TIMEOUT_SECONDS:
            session.clear()
            return

        session["last_seen"] = now

    # -------------- Helper functions ----------------

    def current_user():
        uid = session.get("user_id")
        if not uid:
            return None
        return User.query.get(uid)

    def current_ngo_profile():
        user = current_user()
        if not user or user.role != "ngo":
            return None
        application = NGOApplication.query.filter_by(approved_user_id=user.id, status="approved").first()
        if not application or not application.approved_ngo_id:
            return None
        return NGO.query.get(application.approved_ngo_id)

    def verified_ngos_query():
        """All verified NGOs — includes seeded NGOs and approved applications."""
        return NGO.query.filter_by(is_verified=True)

    @app.context_processor
    def inject_user():
        return {"user": current_user()}

    @app.after_request
    def set_cache_headers(response):
        """Prevent browser from caching authenticated pages.
        Back-button should never show a stale logged-in or logged-out state."""
        # Only apply to HTML responses (not static assets)
        if "text/html" in response.content_type:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def login_required(role=None):
        def decorator(fn):
            from functools import wraps

            @wraps(fn)
            def wrapper(*args, **kwargs):
                user = current_user()
                if not user:
                    flash("Please login first.", "warning")
                    # If this is an admin-only route, send to admin login
                    if role == "admin":
                        return redirect(url_for("admin_login"))
                    return redirect(url_for("login"))

                if role and user.role != role:
                    flash("You do not have permission.", "danger")

                    # If user is admin but trying to access donor-only page
                    if user.role == "admin":
                        return redirect(url_for("admin_dashboard"))

                    # If user is donor but trying to access admin-only page
                    return redirect(url_for("donor_home"))

                return fn(*args, **kwargs)

            return wrapper

        return decorator

    def generate_tracking_id() -> str:
        last = Donation.query.order_by(Donation.id.desc()).first()
        next_number = 1 if not last else last.id + 1
        return f"ZR-{next_number:04d}"

    def fulfill_need(need: NGONeed, quantity: int):
        """Update need qty when NGO marks donation as received."""
        if need and quantity:
            need.qty_fulfilled = min(
                need.qty_required,
                (need.qty_fulfilled or 0) + quantity
            )
            if need.qty_remaining <= 0:
                need.is_active = False

    def build_needs_map(ngos):
        needs_map = {}
        for ngo in ngos:
            need = (
                NGONeed.query
                .filter(
                    NGONeed.ngo_id == ngo.id,
                    NGONeed.is_active == True
                )
                .order_by(NGONeed.created_at.desc())
                .first()
            )
            needs_map[ngo.id] = need
        return needs_map

    def sanitize(value: str, max_length: int = 500) -> str:
        """Strip HTML tags and limit length from any user-submitted text."""
        if not value:
            return ""
        cleaned = re.sub(r"<[^>]*>", "", value)   # strip all HTML tags
        return cleaned[:max_length].strip()

    def send_email(to, subject, html_body):
        """Send an email, silently swallowing errors so mail failures never break the app."""
        if not app.config.get("MAIL_USERNAME"):
            return  # mail not configured — skip silently
        try:
            msg = Message(subject, recipients=[to], html=html_body)
            mail.send(msg)
        except Exception as e:
            app.logger.warning(f"Email send failed to {to}: {e}")

    def build_admin_analytics():
        donations = Donation.query.order_by(Donation.created_at.asc()).all()
        ngos = NGO.query.order_by(NGO.name.asc()).all()
        needs = NGONeed.query.order_by(NGONeed.created_at.desc()).all()

        category_counts = Counter()
        for donation in donations:
            category_counts[(donation.category_manual or "Uncategorized").strip() or "Uncategorized"] += 1

        top_categories = [
            {"label": label, "count": count}
            for label, count in category_counts.most_common(5)
        ]

        active_need_counts = defaultdict(int)
        for need in needs:
            if need.is_active and need.qty_remaining > 0:
                active_need_counts[need.item_name] += need.qty_remaining

        top_need_items = [
            {"label": label, "remaining": remaining}
            for label, remaining in sorted(
                active_need_counts.items(),
                key=lambda item: item[1],
                reverse=True,
            )[:5]
        ]

        month_points = []
        now = datetime.utcnow()
        for offset in range(5, -1, -1):
            month = now.month - offset
            year = now.year
            while month <= 0:
                month += 12
                year -= 1

            count = sum(
                1
                for donation in donations
                if donation.created_at and donation.created_at.year == year and donation.created_at.month == month
            )
            month_points.append({
                "label": datetime(year, month, 1).strftime("%b"),
                "count": count,
            })

        ngo_progress = []
        for ngo in ngos:
            ngo_needs = [need for need in needs if need.ngo_id == ngo.id]
            total_required = sum(need.qty_required or 0 for need in ngo_needs)
            total_fulfilled = sum(need.qty_fulfilled or 0 for need in ngo_needs)
            active_needs = sum(1 for need in ngo_needs if need.is_active and need.qty_remaining > 0)
            fulfillment_pct = int((total_fulfilled / total_required) * 100) if total_required else 0
            ngo_progress.append({
                "ngo": ngo,
                "total_required": total_required,
                "total_fulfilled": total_fulfilled,
                "active_needs": active_needs,
                "fulfillment_pct": min(100, fulfillment_pct),
            })

        ngo_progress.sort(
            key=lambda item: (item["active_needs"], item["total_required"], item["ngo"].name),
            reverse=True,
        )

        return {
            "top_categories": top_categories,
            "top_need_items": top_need_items,
            "monthly_points": month_points,
            "ngo_progress": ngo_progress[:6],
        }

    # -------------- Routes ----------------

    @app.route("/")
    def donor_home():
        user = current_user()
        # If admin is logged in, push them to admin dashboard instead of donor home
        if user and user.role == "admin":
            return redirect(url_for("admin_dashboard"))
        if user and user.role == "ngo":
            return redirect(url_for("ngo_dashboard"))
        if user and user.role == "donor":
            return redirect(url_for("donor_profile"))
        ngos = verified_ngos_query().order_by(NGO.name.asc()).limit(6).all()
        needs_map = build_needs_map(ngos)
        return render_template("donor_home.html", user=user, hide_navbar=True,
                               ngos=ngos, needs_map=needs_map)

    @app.route("/profile", methods=["GET", "POST"])
    @login_required(role="donor")
    def donor_profile():
        user = current_user()
        if not user:
            return redirect(url_for("login"))

        profile_form = {
            "full_name": user.full_name or "",
            "email": user.email or "",
            "phone": user.phone or "",
            "zone": user.zone or "",
        }
        profile_errors = {}
        plan_form = {
            "item_name": "",
            "category": "",
            "quantity": "",
        }
        plan_errors = {}

        if request.method == "POST":
            action = request.form.get("action", "").strip()

            if action == "update_profile":
                full_name = request.form.get("full_name", "").strip()
                email = request.form.get("email", "").strip().lower()
                phone = request.form.get("phone", "").strip()
                zone = request.form.get("zone", "").strip()

                profile_form = {
                    "full_name": full_name,
                    "email": email,
                    "phone": phone,
                    "zone": zone,
                }

                if not full_name:
                    profile_errors["full_name"] = "Full name is required."
                elif len(full_name) < 5:
                    profile_errors["full_name"] = "Enter your full name."

                email_pattern = r'^[A-Za-z][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
                if not email:
                    profile_errors["email"] = "Email is required."
                elif not re.match(email_pattern, email):
                    profile_errors["email"] = "Use a valid email that starts with a letter."
                else:
                    existing_email = User.query.filter(User.email == email, User.id != user.id).first()
                    if existing_email:
                        profile_errors["email"] = "This email is already registered."

                phone_pattern = r'^\d{4}-\d{7}$'
                if not phone:
                    profile_errors["phone"] = "Phone number is required."
                elif not re.match(phone_pattern, phone):
                    profile_errors["phone"] = "Use the format 0301-2345678."
                else:
                    existing_phone = User.query.filter(User.phone == phone, User.id != user.id).first()
                    if existing_phone:
                        profile_errors["phone"] = "This phone number is already registered."

                if not zone:
                    profile_errors["zone"] = "Zone or area is required."

                if not profile_errors:
                    user.full_name = full_name
                    user.email = email
                    user.phone = phone
                    user.zone = zone
                    db.session.commit()
                    flash("Profile updated successfully.", "success")
                    return redirect(url_for("donor_profile"))

            else:
                item_name = request.form.get("item_name", "").strip()
                category = request.form.get("category", "").strip()
                quantity_raw = request.form.get("quantity", "").strip()

                plan_form = {
                    "item_name": item_name,
                    "category": category,
                    "quantity": quantity_raw,
                }

                if not item_name:
                    plan_errors["item_name"] = "Item name is required."

                try:
                    quantity = int(quantity_raw)
                except ValueError:
                    quantity = 0

                if quantity < 1:
                    plan_errors["quantity"] = "Quantity must be a positive number."

                if not plan_errors:
                    plan = DonorAlert(
                        user_id=user.id,
                        item_name=item_name,
                        category=category or None,
                        quantity=quantity,
                        is_active=True,
                    )
                    db.session.add(plan)
                    db.session.commit()
                    flash("Donation plan saved to your profile.", "success")
                    return redirect(url_for("donor_profile"))

        donations = (
            Donation.query
            .filter_by(donor_id=user.id)
            .order_by(Donation.created_at.desc())
            .all()
        )
        plans = (
            DonorAlert.query
            .filter_by(user_id=user.id)
            .order_by(DonorAlert.created_at.desc())
            .all()
        )
        drafts = (
            DonationDraft.query
            .filter_by(user_id=user.id)
            .order_by(DonationDraft.updated_at.desc())
            .all()
        )

        stats = {
            "total":     len(donations),
            "pending":   sum(1 for d in donations if d.status == "pending"),
            "accepted":  sum(1 for d in donations if d.status == "accepted"),
            "received":  sum(1 for d in donations if d.status == "received"),
            "rejected":  sum(1 for d in donations if d.status == "rejected"),
            "cancelled": sum(1 for d in donations if d.status == "cancelled"),
        }

        return render_template(
            "donor_profile.html",
            user=user,
            donations=donations,
            plans=plans,
            drafts=drafts,
            stats=stats,
            profile_form=profile_form,
            profile_errors=profile_errors,
            plan_form=plan_form,
            plan_errors=plan_errors,
        )

    @app.route("/profile/plans/<int:plan_id>/toggle", methods=["POST"])
    @login_required(role="donor")
    def donor_toggle_plan(plan_id):
        user = current_user()
        plan = DonorAlert.query.get_or_404(plan_id)

        if not user or plan.user_id != user.id:
            flash("You do not have permission to update this plan.", "danger")
            return redirect(url_for("donor_profile"))

        plan.is_active = not plan.is_active
        db.session.commit()
        flash("Plan status updated.", "success")
        return redirect(url_for("donor_profile"))

    @app.route("/profile/plans/<int:plan_id>/use")
    @login_required(role="donor")
    def donor_use_plan(plan_id):
        user = current_user()
        plan = DonorAlert.query.get_or_404(plan_id)

        if not user or plan.user_id != user.id:
            flash("You do not have permission to use this plan.", "danger")
            return redirect(url_for("donor_profile"))

        return redirect(url_for(
            "donate",
            item_name=plan.item_name,
            quantity=plan.quantity,
            category_hint=plan.category or "",
        ))

    @app.route("/profile/donations/<int:donation_id>/repeat")
    @login_required(role="donor")
    def donor_repeat_donation(donation_id):
        user = current_user()
        donation = Donation.query.get_or_404(donation_id)

        if not user or donation.donor_id != user.id:
            flash("You do not have permission to reuse this donation.", "danger")
            return redirect(url_for("donor_profile"))

        return redirect(url_for(
            "donate",
            item_name=donation.item_name,
            quantity=donation.quantity or "",
            condition=donation.condition or "",
            category_hint=donation.category_manual or "",
            description=donation.description or "",
        ))

    @app.route("/profile/drafts/<int:draft_id>/use")
    @login_required(role="donor")
    def donor_use_draft(draft_id):
        user = current_user()
        draft = DonationDraft.query.get_or_404(draft_id)

        if not user or draft.user_id != user.id:
            flash("You do not have permission to use this draft.", "danger")
            return redirect(url_for("donor_profile"))

        return redirect(url_for(
            "donate",
            draft_id=draft.id,
            item_name=draft.item_name or "",
            quantity=draft.quantity or "",
            condition=draft.condition or "",
            category_hint=draft.category or "",
            description=draft.description or "",
        ))

    @app.route("/profile/drafts/<int:draft_id>/delete", methods=["POST"])
    @login_required(role="donor")
    def donor_delete_draft(draft_id):
        user = current_user()
        draft = DonationDraft.query.get_or_404(draft_id)

        if not user or draft.user_id != user.id:
            flash("You do not have permission to remove this draft.", "danger")
            return redirect(url_for("donor_profile"))

        db.session.delete(draft)
        db.session.commit()
        flash("Draft removed.", "success")
        return redirect(url_for("donor_profile"))

    @app.route("/profile/plans/<int:plan_id>/delete", methods=["POST"])
    @login_required(role="donor")
    def donor_delete_plan(plan_id):
        user = current_user()
        plan = DonorAlert.query.get_or_404(plan_id)

        if not user or plan.user_id != user.id:
            flash("You do not have permission to remove this plan.", "danger")
            return redirect(url_for("donor_profile"))

        db.session.delete(plan)
        db.session.commit()
        flash("Plan removed.", "success")
        return redirect(url_for("donor_profile"))

    
    @app.route("/ngos")
    def public_ngos():
        q        = request.args.get("q", "").strip()
        city     = request.args.get("city", "").strip()
        category = request.args.get("category", "").strip()
        query    = verified_ngos_query().order_by(NGO.city.asc(), NGO.name.asc())
        if q:
            query = query.filter(
                db.or_(
                    NGO.name.ilike(f"%{q}%"),
                    NGO.city.ilike(f"%{q}%"),
                    NGO.zone.ilike(f"%{q}%"),
                )
            )
        if city:
            query = query.filter(NGO.city.ilike(f"%{city}%"))
        if category:
            query = query.filter(NGO.accepted_categories.ilike(f"%{category}%"))
        page     = request.args.get("page", 1, type=int)
        per_page = 12
        paginated = query.paginate(page=page, per_page=per_page, error_out=False)
        ngos      = paginated.items
        needs_map = build_needs_map(ngos)
        return render_template("list_ngos.html", ngos=ngos, needs_map=needs_map,
                               selected_city=city, selected_category=category, q=q,
                               pagination=paginated)

    @app.route("/ngos/<int:ngo_id>")
    def public_ngo_detail(ngo_id):
        ngo = NGO.query.get_or_404(ngo_id)
        active_needs = (
            NGONeed.query
            .filter_by(ngo_id=ngo.id, is_active=True)
            .order_by(NGONeed.created_at.desc())
            .all()
        )
        archived_needs = (
            NGONeed.query
            .filter_by(ngo_id=ngo.id, is_active=False)
            .order_by(NGONeed.updated_at.desc())
            .limit(6)
            .all()
        )
        received_count = Donation.query.filter_by(ngo_id=ngo.id, status="received").count()
        rated_donations = Donation.query.filter(
            Donation.ngo_id == ngo.id,
            Donation.donor_rating.isnot(None)
        ).all()
        avg_rating = round(sum(d.donor_rating for d in rated_donations) / len(rated_donations), 1) if rated_donations else None

        stats = {"received": received_count}

        return render_template(
            "ngo_detail.html",
            ngo=ngo,
            active_needs=active_needs,
            archived_needs=archived_needs,
            stats=stats,
            avg_rating=avg_rating,
            rating_count=len(rated_donations),
        )

    @app.route("/register", methods=["GET", "POST"])
    def register():
        form_data = {
            "full_name": "",
            "email": "",
            "phone": "",
            "zone": "",
        }

        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            phone = request.form.get("phone", "").strip()
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")
            zone = request.form.get("zone", "").strip()

            form_data = {
                "full_name": full_name,
                "email": email,
                "phone": phone,
                "zone": zone,
            }

            errors = []
            field_errors = {}

            if not full_name:
                field_errors["full_name"] = "Full name is required."
                errors.append(field_errors["full_name"])

            if not email:
                field_errors["email"] = "Email is required."
                errors.append(field_errors["email"])

            if not phone:
                field_errors["phone"] = "Phone number is required."
                errors.append(field_errors["phone"])

            if not zone:
                field_errors["zone"] = "Zone or area in Karachi is required."
                errors.append(field_errors["zone"])

            if not password:
                field_errors["password"] = "Password is required."
                errors.append(field_errors["password"])

            if not confirm_password:
                field_errors["confirm_password"] = "Please confirm your password."
                errors.append(field_errors["confirm_password"])

            email_pattern = r'^[A-Za-z][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
            if email and not re.match(email_pattern, email):
                field_errors["email"] = "Use a valid email that starts with a letter."
                errors.append(field_errors["email"])

            phone_pattern = r'^\d{4}-\d{7}$'
            if phone and not re.match(phone_pattern, phone):
                field_errors["phone"] = "Use the format 0301-2345678."
                errors.append(field_errors["phone"])

            if full_name and len(full_name) < 5:
                field_errors["full_name"] = "Enter your full name."
                errors.append(field_errors["full_name"])

            if password and len(password) < 8:
                field_errors["password"] = "Password must be at least 8 characters long."
                errors.append(field_errors["password"])

            if password and not re.search(r'[^A-Za-z0-9]', password):
                field_errors["password"] = "Password must include at least one special character."
                errors.append(field_errors["password"])

            if password and confirm_password and password != confirm_password:
                field_errors["confirm_password"] = "Passwords do not match."
                if "password" not in field_errors:
                    field_errors["password"] = "Passwords do not match."
                errors.append("Passwords do not match.")

            if email and User.query.filter_by(email=email).first():
                field_errors["email"] = "This email is already registered."
                errors.append(field_errors["email"])

            if phone and User.query.filter_by(phone=phone).first():
                field_errors["phone"] = "This phone number is already registered."
                errors.append(field_errors["phone"])

            if errors:
                return render_template(
                    "register.html",
                    field_errors=field_errors,
                    form_errors=errors,
                    form_data=form_data,
                    hide_navbar=True,
                ), 400

            user = User(full_name=full_name, email=email, phone=phone, zone=zone)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()

            flash("Registration successful. Please login.", "success")
            return redirect(url_for("login"))

        return render_template(
            "register.html",
            field_errors={},
            form_errors=[],
            form_data=form_data,
            hide_navbar=True,
        )

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute;50 per hour", methods=["POST"])
    def login():
        # Already logged in — redirect to appropriate dashboard
        user_now = current_user()
        if user_now:
            if user_now.role == "ngo":
                return redirect(url_for("ngo_dashboard"))
            if user_now.role == "admin":
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("donor_profile", tab="overview"))

        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            field_errors = {}
            form_errors = []

            if not email:
                field_errors["email"] = "Email is required."
                form_errors.append(field_errors["email"])

            if not password:
                field_errors["password"] = "Password is required."
                form_errors.append(field_errors["password"])

            if form_errors:
                return render_template(
                    "login.html",
                    hide_navbar=True,
                    field_errors=field_errors,
                    form_errors=form_errors,
                    form_data={"email": email},
                ), 400

            user = User.query.filter_by(email=email).first()
            if not user or not user.check_password(password):
                return render_template(
                    "login.html",
                    hide_navbar=True,
                    field_errors={"email": "Invalid email or password.", "password": "Invalid email or password."},
                    form_errors=["Invalid email or password."],
                    form_data={"email": email},
                ), 400

            # Block admins here – tell them to use admin login
            if user.role == "admin":
                return render_template(
                    "login.html",
                    hide_navbar=True,
                    field_errors={"email": "Use the admin login for this account."},
                    form_errors=["Use the admin login for this account."],
                    form_data={"email": email},
                ), 400

            if user.role == "ngo":
                return render_template(
                    "login.html",
                    hide_navbar=True,
                    field_errors={"email": "Use the NGO login for this account."},
                    form_errors=["Use the NGO login for this account."],
                    form_data={"email": email},
                ), 400

            # Donor login
            session["user_id"] = user.id
            session["server_instance_id"] = app.config["SERVER_INSTANCE_ID"]
            session["last_seen"] = int(time.time())

            flash("Logged in successfully.", "success")
            return redirect(url_for("donor_profile", tab="overview"))

        return render_template(
            "login.html",
            hide_navbar=True,
            field_errors={},
            form_errors=[],
            form_data={"email": ""},
        )

    
    @app.route("/admin/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute;50 per hour", methods=["POST"])
    def admin_login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            field_errors = {}
            form_errors = []

            if not email:
                field_errors["email"] = "Admin email is required."
                form_errors.append(field_errors["email"])

            if not password:
                field_errors["password"] = "Password is required."
                form_errors.append(field_errors["password"])

            if form_errors:
                return render_template(
                    "admin_login.html",
                    hide_admin_navbar=True,
                    show_admin_header=True,
                    field_errors=field_errors,
                    form_errors=form_errors,
                    form_data={"email": email},
                ), 400

            user = User.query.filter_by(email=email).first()
            if not user or not user.check_password(password):
                return render_template(
                    "admin_login.html",
                    hide_admin_navbar=True,
                    show_admin_header=True,
                    field_errors={"email": "Invalid admin credentials.", "password": "Invalid admin credentials."},
                    form_errors=["Invalid admin credentials."],
                    form_data={"email": email},
                ), 400

            if user.role != "admin":
                return render_template(
                    "admin_login.html",
                    hide_admin_navbar=True,
                    show_admin_header=True,
                    field_errors={"email": "This login is for admin users only."},
                    form_errors=["This login is for admin users only."],
                    form_data={"email": email},
                ), 400

            # login admin
            session["user_id"] = user.id
            session["server_instance_id"] = app.config["SERVER_INSTANCE_ID"]
            session["last_seen"] = int(time.time())

            flash("Logged in as admin.", "success")
            return redirect(url_for("admin_dashboard"))

        return render_template(
            "admin_login.html",
            hide_admin_navbar=True,
            show_admin_header=True,
            field_errors={},
            form_errors=[],
            form_data={"email": ""},
        )

    @app.route("/ngo/register", methods=["GET", "POST"])
    def ngo_register():
        form_data = {
            "organization_name": "",
            "representative_name": "",
            "email": "",
            "phone": "",
            "city": "",
            "zone": "",
            "address": "",
            "accepted_categories": "",
            "details": "",
        }

        if request.method == "POST":
            organization_name = sanitize(request.form.get("organization_name", ""), 200)
            representative_name = sanitize(request.form.get("representative_name", ""), 120)
            email = request.form.get("email", "").strip().lower()
            phone = sanitize(request.form.get("phone", ""), 30)
            city = sanitize(request.form.get("city", ""), 100)
            zone = sanitize(request.form.get("zone", ""), 50)
            address = sanitize(request.form.get("address", ""), 255)
            accepted_categories = ", ".join(request.form.getlist("accepted_categories"))
            details = sanitize(request.form.get("details", ""), 2000)
            password = request.form.get("password", "")
            confirm_password = request.form.get("confirm_password", "")

            form_data = {
                "organization_name": organization_name,
                "representative_name": representative_name,
                "email": email,
                "phone": phone,
                "city": city,
                "zone": zone,
                "address": address,
                "accepted_categories": accepted_categories,
                "details": details,
            }

            field_errors = {}

            if not organization_name:
                field_errors["organization_name"] = "Organization name is required."
            if not representative_name:
                field_errors["representative_name"] = "Representative name is required."
            if not email:
                field_errors["email"] = "Email is required."
            if not phone:
                field_errors["phone"] = "Phone number is required."
            if not city:
                field_errors["city"] = "City is required."
            if not accepted_categories:
                field_errors["accepted_categories"] = "Accepted categories are required."
            if not password:
                field_errors["password"] = "Password is required."
            if not confirm_password:
                field_errors["confirm_password"] = "Please confirm your password."

            email_pattern = r'^[A-Za-z][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'
            if email and not re.match(email_pattern, email):
                field_errors["email"] = "Use a valid email that starts with a letter."

            phone_pattern = r'^\+?\d[\d\-\s]{7,}$'
            if phone and not re.match(phone_pattern, phone):
                field_errors["phone"] = "Use a valid contact number."

            if password and len(password) < 8:
                field_errors["password"] = "Password must be at least 8 characters long."

            if password and not re.search(r'[^A-Za-z0-9]', password):
                field_errors["password"] = "Password must include at least one special character."

            if password and confirm_password and password != confirm_password:
                field_errors["confirm_password"] = "Passwords do not match."

            if email and User.query.filter_by(email=email).first():
                field_errors["email"] = "A user account already exists with this email."

            existing_application = NGOApplication.query.filter_by(email=email).first() if email else None
            if existing_application:
                if existing_application.status == "pending":
                    field_errors["email"] = "An NGO application is already pending for this email."
                elif existing_application.status == "approved":
                    field_errors["email"] = "This NGO account has already been approved."
                else:
                    field_errors["email"] = "This NGO application was already reviewed."

            if organization_name and NGO.query.filter_by(name=organization_name).first():
                field_errors["organization_name"] = "An NGO with this name is already in the system."

            form_errors = list(dict.fromkeys(field_errors.values()))
            if form_errors:
                return render_template(
                    "ngo_register.html",
                    field_errors=field_errors,
                    form_errors=form_errors,
                    form_data=form_data,
                ), 400

            application = NGOApplication(
                organization_name=organization_name,
                representative_name=representative_name,
                email=email,
                phone=phone,
                city=city,
                zone=zone or None,
                address=address or None,
                accepted_categories=accepted_categories,
                details=details or None,
                status="pending",
            )
            application.set_password(password)
            db.session.add(application)
            db.session.commit()

            flash("NGO application submitted. Admin review is required before login.", "success")
            return redirect(url_for("ngo_login"))

        return render_template(
            "ngo_register.html",
            field_errors={},
            form_errors=[],
            form_data=form_data,
        )

    @app.route("/ngo/login", methods=["GET", "POST"])
    @limiter.limit("10 per minute;50 per hour", methods=["POST"])
    def ngo_login():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            field_errors = {}

            if not email:
                field_errors["email"] = "Email is required."
            if not password:
                field_errors["password"] = "Password is required."

            if field_errors:
                return render_template(
                    "ngo_login.html",
                    field_errors=field_errors,
                    form_errors=list(dict.fromkeys(field_errors.values())),
                    form_data={"email": email},
                ), 400

            user = User.query.filter_by(email=email).first()
            if not user or not user.check_password(password):
                return render_template(
                    "ngo_login.html",
                    field_errors={"email": "Invalid NGO credentials.", "password": "Invalid NGO credentials."},
                    form_errors=["Invalid NGO credentials."],
                    form_data={"email": email},
                ), 400

            if user.role != "ngo":
                return render_template(
                    "ngo_login.html",
                    field_errors={"email": "This login is for approved NGO accounts only."},
                    form_errors=["This login is for approved NGO accounts only."],
                    form_data={"email": email},
                ), 400

            session["user_id"] = user.id
            session["server_instance_id"] = app.config["SERVER_INSTANCE_ID"]
            session["last_seen"] = int(time.time())

            flash("Logged in successfully.", "success")
            return redirect(url_for("ngo_dashboard"))

        return render_template(
            "ngo_login.html",
            field_errors={},
            form_errors=[],
            form_data={"email": ""},
        )

    def build_ngo_dashboard_context(ngo):
        needs = (
            NGONeed.query
            .filter_by(ngo_id=ngo.id)
            .order_by(NGONeed.created_at.desc())
            .all()
        )
        donations = (
            Donation.query
            .filter(Donation.ngo_id == ngo.id)
            .order_by(Donation.created_at.desc())
            .all()
        )
        stats = {
            "pending":      sum(1 for d in donations if d.status == "pending"),
            "accepted":     sum(1 for d in donations if d.status == "accepted"),
            "received":     sum(1 for d in donations if d.status == "received"),
            "active_needs": sum(1 for n in needs if n.is_active and n.qty_remaining > 0),
            "fulfilled_qty": sum((n.qty_fulfilled or 0) for n in needs),
            "required_qty": sum((n.qty_required or 0) for n in needs),
        }
        return {
            "needs": needs,
            "donations": donations,
            "recent_needs": needs[:3],
            "recent_donations": [d for d in donations if d.status in ("pending", "accepted")][:5],
            "stats": stats,
        }

    @app.route("/ngo/dashboard")
    @login_required(role="ngo")
    def ngo_dashboard():
        user = current_user()
        ngo = current_ngo_profile()

        if not ngo:
            flash("This NGO account is not linked properly. Please contact admin.", "danger")
            return redirect(url_for("logout"))
        dashboard_context = build_ngo_dashboard_context(ngo)

        return render_template(
            "ngo_dashboard.html",
            ngo=ngo,
            ngo_user=user,
            **dashboard_context,
        )

    @app.route("/ngo/needs", methods=["GET", "POST"])
    @login_required(role="ngo")
    def ngo_needs():
        ngo = current_ngo_profile()
        if not ngo:
            flash("This NGO account is not linked properly. Please contact admin.", "danger")
            return redirect(url_for("logout"))

        if request.method == "POST":
            item_name = request.form.get("item_name", "").strip()
            category = request.form.get("category", "").strip()
            condition_needed = request.form.get("condition_needed", "").strip()
            details = request.form.get("details", "").strip()

            qty_required_raw = request.form.get("qty_required", "0").strip()
            try:
                qty_required = int(qty_required_raw)
            except ValueError:
                qty_required = -1

            if not item_name:
                flash("Item name is required.", "danger")
                return redirect(url_for("ngo_needs"))

            if qty_required < 1:
                flash("Required quantity must be a positive number.", "danger")
                return redirect(url_for("ngo_needs"))

            need = NGONeed(
                ngo_id=ngo.id,
                item_name=item_name,
                category=category or None,
                condition_needed=condition_needed or None,
                details=details or None,
                qty_required=qty_required,
                qty_fulfilled=0,
                is_active=True,
            )
            db.session.add(need)
            db.session.commit()

            flash("Need added successfully.", "success")
            return redirect(url_for("ngo_needs"))

        dashboard_context = build_ngo_dashboard_context(ngo)
        return render_template(
            "ngo_needs.html",
            ngo=ngo,
            **dashboard_context,
        )

    @app.route("/ngo/donations")
    @login_required(role="ngo")
    def ngo_donations():
        ngo = current_ngo_profile()
        if not ngo:
            flash("This NGO account is not linked properly. Please contact admin.", "danger")
            return redirect(url_for("logout"))
        all_donations = (
            Donation.query
            .filter(Donation.ngo_id == ngo.id)
            .order_by(Donation.created_at.desc())
            .all()
        )
        return render_template(
            "ngo_assigned_donations.html",
            ngo=ngo,
            pending_donations  = [d for d in all_donations if d.status == "pending"],
            accepted_donations = [d for d in all_donations if d.status == "accepted"],
            received_donations = [d for d in all_donations if d.status == "received"],
            rejected_donations = [d for d in all_donations if d.status == "rejected"],
        )

    @app.route("/ngo/donations/<int:donation_id>/accept", methods=["POST"])
    @login_required(role="ngo")
    def ngo_accept_donation(donation_id):
        ngo = current_ngo_profile()
        donation = Donation.query.get_or_404(donation_id)

        if not ngo or donation.ngo_id != ngo.id:
            flash("You do not have permission.", "danger")
            return redirect(url_for("ngo_donations"))

        if donation.status != "pending":
            flash("Only pending donations can be accepted.", "warning")
            return redirect(url_for("ngo_donations"))

        # Capacity check
        if ngo.max_active_donations:
            current_accepted = Donation.query.filter_by(ngo_id=ngo.id, status="accepted").count()
            if current_accepted >= ngo.max_active_donations:
                flash(
                    f"Capacity limit reached ({ngo.max_active_donations} active donations). "
                    "Mark some donations as received before accepting new ones.",
                    "warning"
                )
                return redirect(url_for("ngo_donations"))

        donation.status = "accepted"
        donation.accepted_at = datetime.utcnow()
        db.session.commit()

        # Email: notify donor their donation was accepted
        if donation.donor:
            send_email(
                donation.donor.email,
                f"Your Donation Was Accepted — {donation.tracking_id}",
                render_template("emails/donation_accepted.html",
                    donation=donation, ngo=ngo)
            )

        flash("Donation accepted. Please contact the donor to arrange pickup.", "success")
        return redirect(url_for("ngo_donations"))

    @app.route("/ngo/donations/<int:donation_id>/decline", methods=["POST"])
    @login_required(role="ngo")
    def ngo_decline_donation(donation_id):
        ngo = current_ngo_profile()
        donation = Donation.query.get_or_404(donation_id)

        if not ngo or donation.ngo_id != ngo.id:
            flash("You do not have permission.", "danger")
            return redirect(url_for("ngo_donations"))

        if donation.status != "pending":
            flash("Only pending donations can be declined.", "warning")
            return redirect(url_for("ngo_donations"))

        reason = request.form.get("reason", "").strip()
        donation.status = "rejected"
        donation.rejected_reason = reason or f"Declined by {ngo.name}"
        donation.rejected_at = datetime.utcnow()
        db.session.commit()

        flash("Donation declined.", "info")
        return redirect(url_for("ngo_donations"))

    @app.route("/ngo/donations/<int:donation_id>/received", methods=["POST"])
    @login_required(role="ngo")
    def ngo_mark_received(donation_id):
        ngo = current_ngo_profile()
        donation = Donation.query.get_or_404(donation_id)

        if not ngo or donation.ngo_id != ngo.id:
            flash("You do not have permission.", "danger")
            return redirect(url_for("ngo_donations"))

        if donation.status != "accepted":
            flash("Only accepted donations can be marked as received.", "warning")
            return redirect(url_for("ngo_donations"))

        donation.status = "received"
        donation.received_at = datetime.utcnow()

        if donation.need:
            fulfill_need(donation.need, donation.quantity or 0)

        db.session.commit()

        # Email: notify donor pickup is confirmed
        if donation.donor:
            send_email(
                donation.donor.email,
                f"Pickup Confirmed — {donation.tracking_id}",
                render_template("emails/donation_received.html",
                    donation=donation, ngo=ngo)
            )

        flash("Marked as received. Thank you!", "success")
        return redirect(url_for("ngo_donations"))

    @app.route("/ngo/needs/<int:need_id>/toggle", methods=["POST"])
    @login_required(role="ngo")
    def ngo_toggle_need(need_id):
        ngo = current_ngo_profile()
        need = NGONeed.query.get_or_404(need_id)

        if not ngo or need.ngo_id != ngo.id:
            flash("You do not have permission to update this need.", "danger")
            return redirect(url_for("ngo_needs"))

        need.is_active = not need.is_active
        db.session.commit()
        flash("Need status updated.", "success")
        return redirect(url_for("ngo_needs"))


    @app.route("/logout")
    def logout():
        if not session.get("user_id"):
            flash("You are not logged in.", "danger")
            return redirect(url_for("donor_home"))

        session.clear()
        flash("Logged out.", "info")
        return redirect(url_for("donor_home"))


    @app.route("/donate/new", methods=["GET", "POST"])
    @login_required(role="donor")
    def donate():
        user = current_user()

        # Pre-fill from browse (NGO profile "Donate This" button)
        ngo_id_hint  = request.args.get("ngo_id", "").strip()
        need_id_hint = request.args.get("need_id", "").strip()
        draft_id_hint = request.args.get("draft_id", "").strip()

        selected_ngo  = NGO.query.get(int(ngo_id_hint))  if ngo_id_hint.isdigit()  else None
        selected_need = NGONeed.query.get(int(need_id_hint)) if need_id_hint.isdigit() else None

        form_data = {
            "draft_id":      draft_id_hint,
            "ngo_id":        ngo_id_hint,
            "need_id":       need_id_hint,
            "item_name":     (selected_need.item_name if selected_need else request.args.get("item_name", "")).strip(),
            "quantity":      request.args.get("quantity", "").strip(),
            "condition":     request.args.get("condition", "").strip(),
            "category":      (selected_need.category if selected_need else request.args.get("category", "")).strip(),
            "description":   request.args.get("description", "").strip(),
            "pickup_address": user.zone or "",
            "pickup_notes":  "",
        }

        ngos      = verified_ngos_query().order_by(NGO.city.asc(), NGO.name.asc()).all()
        needs_map = build_needs_map(ngos)

        def render_form(field_errors=None, form_errors=None):
            return render_template(
                "donation_form.html",
                field_errors=field_errors or {},
                form_errors=form_errors or [],
                form_data=form_data,
                ngos=ngos,
                needs_map=needs_map,
                selected_ngo=selected_ngo,
                selected_need=selected_need,
            )

        if request.method == "POST":
            action        = request.form.get("action", "submit").strip()
            draft_id_raw  = request.form.get("draft_id", "").strip()
            ngo_id_raw    = request.form.get("ngo_id", "").strip()
            need_id_raw   = request.form.get("need_id", "").strip()
            item_name     = sanitize(request.form.get("item_name", ""), 200)
            quantity      = request.form.get("quantity", "").strip()
            condition     = request.form.get("condition", "").strip()
            category      = sanitize(request.form.get("category", ""), 100)
            description   = sanitize(request.form.get("description", ""), 1000)
            # Structured address fields → assemble into one string
            house  = sanitize(request.form.get("pickup_house", ""), 100)
            street = sanitize(request.form.get("pickup_street", ""), 100)
            area   = sanitize(request.form.get("pickup_area", ""), 100)
            city   = sanitize(request.form.get("pickup_city", ""), 100)
            pickup_address = ", ".join(p for p in [house, street, area, city] if p)
            pickup_notes  = sanitize(request.form.get("pickup_notes", ""), 500)

            form_data.update({
                "draft_id": draft_id_raw, "ngo_id": ngo_id_raw, "need_id": need_id_raw,
                "item_name": item_name, "quantity": quantity, "condition": condition,
                "category": category, "description": description,
                "pickup_house": house, "pickup_street": street,
                "pickup_area": area, "pickup_city": city,
                "pickup_address": pickup_address, "pickup_notes": pickup_notes,
            })

            # Resolve NGO and need from submitted IDs
            selected_ngo  = NGO.query.get(int(ngo_id_raw))  if ngo_id_raw.isdigit()  else None
            selected_need = NGONeed.query.get(int(need_id_raw)) if need_id_raw.isdigit() else None

            # ---- Save draft ----
            if action == "save_draft":
                if not any([item_name, quantity, condition, description]):
                    return render_form({"item_name": "Add some details before saving a draft."},
                                       ["Add some details before saving a draft."]), 400
                quantity_int = int(quantity) if quantity.isdigit() and int(quantity) > 0 else None
                draft = DonationDraft.query.get(int(draft_id_raw)) if draft_id_raw.isdigit() else None
                if draft and draft.user_id != user.id:
                    draft = None
                if not draft:
                    draft = DonationDraft(user_id=user.id)
                    db.session.add(draft)
                draft.item_name  = item_name or None
                draft.quantity   = quantity_int
                draft.condition  = condition or None
                draft.description = description or None
                draft.category   = category or None
                db.session.commit()
                flash("Draft saved.", "success")
                return redirect(url_for("donor_profile"))

            # ---- Validate submission ----
            field_errors = {}
            errors = []

            if not selected_ngo:
                field_errors["ngo_id"] = "Please select an NGO."
                errors.append(field_errors["ngo_id"])

            if not item_name:
                field_errors["item_name"] = "Item name is required."
                errors.append(field_errors["item_name"])
            elif len(item_name) < 3 or not re.search(r"[A-Za-z]", item_name):
                field_errors["item_name"] = "Enter a meaningful item name."
                errors.append(field_errors["item_name"])

            if not quantity or not quantity.isdigit() or int(quantity) <= 0:
                field_errors["quantity"] = "Enter a valid quantity (positive number)."
                errors.append(field_errors["quantity"])

            if not condition:
                field_errors["condition"] = "Select the item condition."
                errors.append(field_errors["condition"])

            if not category:
                field_errors["category"] = "Select a donation category."
                errors.append(field_errors["category"])
            elif selected_ngo and selected_ngo.accepted_categories:
                ngo_cats = [c.strip().lower() for c in selected_ngo.accepted_categories.split(",")]
                if category.lower() not in ngo_cats:
                    field_errors["category"] = f"{selected_ngo.name} only accepts: {selected_ngo.accepted_categories}."
                    errors.append(field_errors["category"])

            def addr_gibberish(s):
                """True if the string looks like gibberish — too short, too few unique chars, or no letters."""
                s2 = s.replace(" ", "").lower()
                if len(s2) < 2:
                    return True
                if len(set(s2)) < 2:        # e.g. "aaaa"
                    return True
                if not re.search(r"[a-z]", s2):  # must have at least one letter
                    return True
                return False

            PAKISTANI_CITIES = {
                "karachi","lahore","islamabad","rawalpindi","faisalabad","multan",
                "peshawar","quetta","sialkot","gujranwala","hyderabad","abbottabad",
                "bahawalpur","sargodha","sukkur","larkana","sheikhupura","jhang",
                "rahim yar khan","gujrat","sahiwal","mardan","mingora","nawabshah",
                "okara","mirpur khas","dera ghazi khan","chiniot","kamoke","hafizabad",
                "sadiqabad","jacobabad","khanewal","kohat","turbat","muzaffarabad",
                "mirpur","kotri","jaranwala","daska","muridke","bahawalnagar",
            }

            if not house:
                field_errors["pickup_house"] = "House or flat number is required."
                errors.append(field_errors["pickup_house"])
            elif not re.search(r"\d", house):
                field_errors["pickup_house"] = "Include a number in your house/flat (e.g. House 5, Flat 3B)."
                errors.append(field_errors["pickup_house"])

            if not street:
                field_errors["pickup_street"] = "Street name is required."
                errors.append(field_errors["pickup_street"])
            elif len(street) < 4 or addr_gibberish(street):
                field_errors["pickup_street"] = "Enter a real street name (e.g. Street 4, Main Boulevard)."
                errors.append(field_errors["pickup_street"])

            if not area:
                field_errors["pickup_area"] = "Area or neighbourhood is required."
                errors.append(field_errors["pickup_area"])
            elif len(area) < 4 or addr_gibberish(area):
                field_errors["pickup_area"] = "Enter a real area name (e.g. Gulshan-e-Iqbal, DHA Phase 5)."
                errors.append(field_errors["pickup_area"])

            if not city:
                field_errors["pickup_city"] = "City is required."
                errors.append(field_errors["pickup_city"])
            elif city.lower().strip() not in PAKISTANI_CITIES:
                field_errors["pickup_city"] = "Enter a valid Pakistani city (e.g. Karachi, Lahore, Islamabad)."
                errors.append(field_errors["pickup_city"])
            elif selected_ngo and selected_ngo.city.lower().strip() != city.lower().strip():
                field_errors["pickup_city"] = (
                    f"{selected_ngo.name} is based in {selected_ngo.city}. "
                    f"Your pickup address must be in {selected_ngo.city}."
                )
                errors.append(field_errors["pickup_city"])

            # Block cash and blood donations
            combined = f"{item_name} {description}".lower()
            if any(w in combined for w in ["cash", "money", "zakat", "sadqa", "fund", "amount"]):
                field_errors["item_name"] = "Cash or monetary donations are not accepted."
                errors.append(field_errors["item_name"])
            if any(w in combined for w in ["blood", "khoon"]):
                field_errors["item_name"] = "Blood donations are not handled here."
                errors.append(field_errors["item_name"])

            if errors:
                return render_form(field_errors, errors), 400

            quantity_int = int(quantity)

            # Block exact duplicates still in progress
            duplicate = Donation.query.filter(
                Donation.donor_id == user.id,
                Donation.item_name.ilike(item_name),
                Donation.quantity == quantity_int,
                Donation.condition == condition,
                Donation.ngo_id == selected_ngo.id,
                Donation.status.in_(["pending", "accepted"])
            ).first()
            if duplicate:
                return render_form(
                    {"item_name": "A similar donation to this NGO is already in progress."},
                    ["You already have a similar active donation to this NGO."]
                ), 400

            tracking_id = generate_tracking_id()
            donation = Donation(
                tracking_id    = tracking_id,
                item_name      = item_name,
                category_manual = category,
                quantity       = quantity_int,
                condition      = condition,
                description    = description or None,
                pickup_address = pickup_address,
                pickup_notes   = pickup_notes or None,
                status         = "pending",
                donor_id       = user.id,
                ngo_id         = selected_ngo.id,
                need_id        = selected_need.id if selected_need and selected_need.ngo_id == selected_ngo.id else None,
            )
            db.session.add(donation)

            # Delete draft if used
            if draft_id_raw.isdigit():
                draft = DonationDraft.query.get(int(draft_id_raw))
                if draft and draft.user_id == user.id:
                    db.session.delete(draft)

            db.session.commit()

            # Email: confirm submission to donor
            send_email(
                user.email,
                f"Donation Submitted — {tracking_id}",
                render_template("emails/donation_submitted.html",
                    user=user, donation=donation, ngo=selected_ngo)
            )

            flash(f"Donation submitted to {selected_ngo.name}. They will review and arrange pickup.", "success")
            return redirect(url_for("donation_success", tracking_id=tracking_id))

        return render_form()

    @app.route("/donation/success/<tracking_id>")
    @login_required(role="donor")
    def donation_success(tracking_id):
        donation = Donation.query.filter_by(tracking_id=tracking_id, donor_id=session.get("user_id")).first_or_404()
        return render_template("donation_success.html", tracking_id=tracking_id, donation=donation)

    @app.route("/donations/<int:donation_id>/cancel", methods=["POST"])
    @login_required(role="donor")
    def donor_cancel_donation(donation_id):
        user = current_user()
        donation = Donation.query.get_or_404(donation_id)

        if donation.donor_id != user.id:
            flash("You do not have permission to cancel this donation.", "danger")
            return redirect(url_for("donor_profile"))

        if donation.status not in ("pending",):
            flash("Only pending donations can be cancelled.", "warning")
            return redirect(url_for("donor_profile"))

        donation.status = "cancelled"
        donation.cancelled_at = datetime.utcnow()
        db.session.commit()
        flash("Donation cancelled.", "info")
        return redirect(url_for("donor_profile"))

    # ---------- Complaints ----------

    @app.route("/complaints/new", methods=["GET", "POST"])
    @login_required()
    def submit_complaint():
        user = current_user()
        user_donations = Donation.query.filter_by(donor_id=user.id).order_by(Donation.created_at.desc()).all() \
            if user.role == "donor" else []

        if request.method == "POST":
            subject     = sanitize(request.form.get("subject", ""), 200)
            message     = sanitize(request.form.get("message", ""), 2000)
            donation_id = request.form.get("donation_id", "").strip()

            field_errors = {}
            if not subject:
                field_errors["subject"] = "Subject is required."
            if not message or len(message) < 20:
                field_errors["message"] = "Please describe your complaint in at least 20 characters."

            if field_errors:
                return render_template("complaint_form.html", field_errors=field_errors,
                                       user_donations=user_donations, form_data=request.form), 400

            linked_donation = Donation.query.get(int(donation_id)) if donation_id.isdigit() else None

            complaint = Complaint(
                user_id     = user.id,
                donation_id = linked_donation.id if linked_donation else None,
                subject     = subject,
                message     = message,
            )
            db.session.add(complaint)
            db.session.commit()
            flash("Your complaint has been submitted. Admin will review it shortly.", "success")
            return redirect(url_for("donor_profile") if user.role == "donor" else url_for("ngo_dashboard"))

        return render_template("complaint_form.html", field_errors={},
                               user_donations=user_donations, form_data={})

    # ---------- Password Reset ----------

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password():
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            if not email:
                return render_template("forgot_password.html",
                    error="Email is required."), 400

            user = User.query.filter_by(email=email).first()
            # Always show success to prevent email enumeration
            if user:
                # Invalidate any existing tokens for this user
                PasswordResetToken.query.filter_by(user_id=user.id, used=False).update({"used": True})
                db.session.flush()

                token = secrets.token_urlsafe(32)
                reset_token = PasswordResetToken(
                    user_id    = user.id,
                    token      = token,
                    expires_at = datetime.utcnow() + timedelta(hours=1),
                )
                db.session.add(reset_token)
                db.session.commit()

                reset_url = url_for("reset_password", token=token, _external=True)
                send_email(
                    user.email,
                    "Reset Your Zariya Password",
                    render_template("emails/password_reset.html",
                        user=user, reset_url=reset_url)
                )

            return render_template("forgot_password.html", sent=True)

        return render_template("forgot_password.html")

    @app.route("/reset-password/<token>", methods=["GET", "POST"])
    def reset_password(token):
        reset_token = PasswordResetToken.query.filter_by(token=token, used=False).first()

        if not reset_token or reset_token.expires_at < datetime.utcnow():
            return render_template("reset_password.html", invalid=True)

        if request.method == "POST":
            password = request.form.get("password", "")
            confirm  = request.form.get("confirm_password", "")

            if not password or len(password) < 8:
                return render_template("reset_password.html", token=token,
                    error="Password must be at least 8 characters."), 400
            if password != confirm:
                return render_template("reset_password.html", token=token,
                    error="Passwords do not match."), 400

            reset_token.user.set_password(password)
            reset_token.used = True
            db.session.commit()

            # Clear any active sessions for this user (force re-login)
            flash("Password updated successfully. Please login with your new password.", "success")
            role = reset_token.user.role
            if role == "ngo":
                return redirect(url_for("ngo_login"))
            if role == "admin":
                return redirect(url_for("admin_login"))
            return redirect(url_for("login"))

        return render_template("reset_password.html", token=token)

    # ---------- Tracking ----------
    @app.route("/track", methods=["GET", "POST"])
    @login_required(role="donor")
    def track():
        user = current_user()
        if not user:
            return redirect(url_for("login"))

        if request.method == "POST":
            tracking_id = request.form.get("tracking_id", "").strip()
            if not tracking_id:
                return render_template(
                    "track_form.html",
                    field_errors={"tracking_id": "Enter your tracking ID."},
                    form_errors=["Enter your tracking ID."],
                    form_data={"tracking_id": tracking_id},
                ), 400

            # Only allow tracking of donations belonging to this logged-in user
            donation = Donation.query.filter_by(
                tracking_id=tracking_id,
                donor_id=user.id
            ).first()

            if not donation:
                return render_template(
                    "track_form.html",
                    field_errors={"tracking_id": "No donation was found for this tracking ID on your account."},
                    form_errors=["No donation was found for this tracking ID on your account."],
                    form_data={"tracking_id": tracking_id},
                ), 404

            return render_template("track_result.html", donation=donation)

        return render_template(
            "track_form.html",
            field_errors={},
            form_errors=[],
            form_data={"tracking_id": ""},
        )

    # ---------- NGO Profile Edit ----------

    @app.route("/ngo/profile/edit", methods=["GET", "POST"])
    @login_required(role="ngo")
    def ngo_edit_profile():
        ngo = current_ngo_profile()
        if not ngo:
            flash("NGO profile not found.", "danger")
            return redirect(url_for("ngo_dashboard"))

        if request.method == "POST":
            name    = request.form.get("name", "").strip()
            city    = request.form.get("city", "").strip()
            zone    = request.form.get("zone", "").strip()
            address = request.form.get("address", "").strip()
            phone   = request.form.get("phone", "").strip()
            description = request.form.get("description", "").strip()
            accepted_categories = ", ".join(request.form.getlist("accepted_categories"))
            max_donations_raw = request.form.get("max_active_donations", "").strip()

            field_errors = {}
            if not name:
                field_errors["name"] = "Organization name is required."
            if not city:
                field_errors["city"] = "City is required."

            max_active_donations = None
            if max_donations_raw:
                try:
                    max_active_donations = int(max_donations_raw)
                    if max_active_donations < 1:
                        field_errors["max_active_donations"] = "Capacity must be at least 1."
                        max_active_donations = None
                except ValueError:
                    field_errors["max_active_donations"] = "Enter a valid number."

            if field_errors:
                return render_template("ngo_edit_profile.html",
                    ngo=ngo, field_errors=field_errors), 400

            ngo.name                = name
            ngo.city                = city
            ngo.zone                = zone or None
            ngo.address             = address or None
            ngo.contact_phone       = phone or None
            ngo.description         = description or None
            ngo.accepted_categories = accepted_categories or None
            ngo.max_active_donations = max_active_donations
            db.session.commit()

            flash("Profile updated successfully.", "success")
            return redirect(url_for("ngo_dashboard"))

        return render_template("ngo_edit_profile.html", ngo=ngo, field_errors={})

    # ---------- Admin ----------

    @app.route("/admin/dashboard")
    @login_required(role="admin")
    def admin_dashboard():
        pending  = Donation.query.filter_by(status="pending").order_by(Donation.created_at.asc()).all()
        accepted = Donation.query.filter_by(status="accepted").order_by(Donation.accepted_at.desc()).all()
        received = Donation.query.filter_by(status="received").order_by(Donation.received_at.desc()).all()
        rejected = Donation.query.filter_by(status="rejected").order_by(Donation.rejected_at.desc()).all()
        ngos     = NGO.query.order_by(NGO.name.asc()).all()
        open_complaints = Complaint.query.filter_by(status="open").order_by(Complaint.created_at.asc()).all()
        pending_ngo_applications = (
            NGOApplication.query.filter_by(status="pending")
            .order_by(NGOApplication.created_at.asc()).all()
        )
        stats = {
            "pending":              len(pending),
            "accepted":             len(accepted),
            "received":             len(received),
            "open_complaints":      len(open_complaints),
            "pending_applications": len(pending_ngo_applications),
        }
        return render_template(
            "admin_dashboard.html",
            stats=stats,
            recent_donations=pending[:5],
            pending_applications=pending_ngo_applications[:5],
            open_complaints=open_complaints[:5],
            ngos=ngos,
            show_admin_header=True,
            hide_admin_navbar=False,
        )

    @app.route("/admin/ngo-applications")
    @login_required(role="admin")
    def admin_ngo_applications():
        applications = (
            NGOApplication.query
            .order_by(
                NGOApplication.status.asc(),
                NGOApplication.created_at.desc()
            )
            .all()
        )
        return render_template(
            "admin_ngo_applications.html",
            applications=applications,
            show_admin_header=True,
            hide_admin_navbar=False,
        )

    @app.route("/admin/ngo-applications/<int:application_id>", methods=["GET", "POST"])
    @login_required(role="admin")
    def admin_ngo_application_detail(application_id):
        application = NGOApplication.query.get_or_404(application_id)

        if request.method == "POST":
            action = request.form.get("action", "").strip()
            admin_notes = request.form.get("admin_notes", "").strip()
            application.admin_notes = admin_notes or None
            application.reviewed_at = datetime.utcnow()

            if action == "approve":
                if application.status == "approved":
                    flash("This NGO application is already approved.", "info")
                    return redirect(url_for("admin_ngo_application_detail", application_id=application.id))

                existing_user = User.query.filter_by(email=application.email).first()
                if existing_user and existing_user.role != "ngo":
                    flash("This email is already in use by another account type.", "danger")
                    return redirect(url_for("admin_ngo_application_detail", application_id=application.id))

                user = existing_user
                if not user:
                    user = User(
                        full_name=application.representative_name,
                        email=application.email,
                        phone=application.phone,
                        zone=application.zone,
                        role="ngo",
                    )
                    user.password_hash = application.password_hash
                    db.session.add(user)
                    db.session.flush()

                ngo = NGO.query.get(application.approved_ngo_id) if application.approved_ngo_id else None
                if not ngo:
                    ngo = NGO(
                        name=application.organization_name,
                        city=application.city,
                        zone=application.zone,
                        address=application.address,
                        contact_email=application.email,
                        contact_phone=application.phone,
                        accepted_categories=application.accepted_categories,
                        is_verified=True,
                        has_pickup=True,
                    )
                    db.session.add(ngo)
                    db.session.flush()

                application.status = "approved"
                application.approved_user_id = user.id
                application.approved_ngo_id = ngo.id

                db.session.commit()
                flash("NGO application approved and NGO account created.", "success")
                return redirect(url_for("admin_ngo_applications"))

            if action == "reject":
                application.status = "rejected"
                db.session.commit()
                flash("NGO application rejected.", "info")
                return redirect(url_for("admin_ngo_applications"))

        return render_template(
            "admin_ngo_application_detail.html",
            application=application,
            show_admin_header=True,
            hide_admin_navbar=False,
        )

    @app.route("/admin/analytics")
    @login_required(role="admin")
    def admin_analytics():
        analytics = build_admin_analytics()
        return render_template(
            "admin_analytics.html",
            analytics=analytics,
            show_admin_header=True,
            hide_admin_navbar=False,
        )
    
    @app.route("/admin/donations/pending")
    @login_required(role="admin")
    def admin_pending_donations():
        page = request.args.get("page", 1, type=int)
        status_filter = request.args.get("status", "")
        q = request.args.get("q", "").strip()
        query = Donation.query
        if status_filter:
            query = query.filter_by(status=status_filter)
        else:
            query = query.filter_by(status="pending")
        if q:
            query = query.filter(
                db.or_(Donation.tracking_id.ilike(f"%{q}%"),
                       Donation.item_name.ilike(f"%{q}%"))
            )
        paginated = query.order_by(Donation.created_at.asc()).paginate(page=page, per_page=25, error_out=False)
        return render_template("admin_donations_list.html", donations=paginated.items,
                               pagination=paginated,
                               page_title="Pending Donations", status_label="Pending",
                               show_admin_header=True, hide_admin_navbar=False)

    @app.route("/admin/donations/accepted")
    @login_required(role="admin")
    def admin_accepted_donations():
        page = request.args.get("page", 1, type=int)
        paginated = Donation.query.filter_by(status="accepted").order_by(
            Donation.accepted_at.desc()).paginate(page=page, per_page=25, error_out=False)
        return render_template("admin_donations_list.html", donations=paginated.items,
                               pagination=paginated,
                               page_title="Accepted — Awaiting Pickup", status_label="Accepted",
                               show_admin_header=True, hide_admin_navbar=False)

    @app.route("/admin/donations/received")
    @login_required(role="admin")
    def admin_received_donations():
        page = request.args.get("page", 1, type=int)
        paginated = Donation.query.filter_by(status="received").order_by(
            Donation.received_at.desc()).paginate(page=page, per_page=25, error_out=False)
        return render_template("admin_donations_list.html", donations=paginated.items,
                               pagination=paginated,
                               page_title="Received Donations", status_label="Received",
                               show_admin_header=True, hide_admin_navbar=False)

    @app.route("/admin/donations/rejected")
    @login_required(role="admin")
    def admin_rejected_donations():
        page = request.args.get("page", 1, type=int)
        paginated = Donation.query.filter_by(status="rejected").order_by(
            Donation.rejected_at.desc()).paginate(page=page, per_page=25, error_out=False)
        return render_template("admin_donations_list.html", donations=paginated.items,
                               pagination=paginated,
                               page_title="Rejected Donations", status_label="Rejected",
                               show_admin_header=True, hide_admin_navbar=False)

    @app.route("/admin/complaints")
    @login_required(role="admin")
    def admin_complaints():
        complaints = Complaint.query.order_by(Complaint.status.asc(), Complaint.created_at.asc()).all()
        return render_template("admin_complaints.html", complaints=complaints,
                               show_admin_header=True, hide_admin_navbar=False)

    @app.route("/admin/complaints/<int:complaint_id>/resolve", methods=["POST"])
    @login_required(role="admin")
    def admin_resolve_complaint(complaint_id):
        complaint = Complaint.query.get_or_404(complaint_id)
        complaint.status = "resolved"
        complaint.admin_notes = request.form.get("admin_notes", "").strip() or None
        complaint.resolved_at = datetime.utcnow()
        db.session.commit()
        flash("Complaint marked as resolved.", "success")
        return redirect(url_for("admin_complaints"))

    
    @app.route("/admin/donation/<int:donation_id>", methods=["GET", "POST"])
    @login_required(role="admin")
    def admin_donation_detail(donation_id):
        donation = Donation.query.get_or_404(donation_id)

        if request.method == "POST":
            action = request.form.get("action")

            if action == "reject":
                reason = request.form.get("reject_reason", "").strip()

                if not reason:
                    flash("Reject reason is required.", "danger")
                    return redirect(url_for("admin_donation_detail", donation_id=donation_id))

                donation.status = "rejected"
                donation.rejected_reason = reason
                donation.rejected_at = datetime.utcnow()

                db.session.commit()
                flash("Donation rejected.", "info")
                return redirect(url_for("admin_dashboard"))

            if action == "reassign":
                if donation.status in {"received", "cancelled"}:
                    flash("This donation cannot be reassigned.", "warning")
                    return redirect(url_for("admin_donation_detail", donation_id=donation_id))

                ngo_id_raw  = request.form.get("ngo_id", "").strip()
                need_id_raw = request.form.get("need_id", "").strip()
                ngo = NGO.query.get(int(ngo_id_raw)) if ngo_id_raw.isdigit() else None
                if not ngo:
                    flash("Please choose a valid NGO.", "danger")
                    return redirect(url_for("admin_donation_detail", donation_id=donation_id))

                need = None
                if need_id_raw.isdigit():
                    need = NGONeed.query.get(int(need_id_raw))
                    if not need or need.ngo_id != ngo.id:
                        flash("The selected need does not belong to this NGO.", "danger")
                        return redirect(url_for("admin_donation_detail", donation_id=donation_id))

                donation.ngo_id    = ngo.id
                donation.need_id   = need.id if need else None
                donation.status    = "pending"
                donation.accepted_at = None
                db.session.commit()
                flash(f"Donation reassigned to {ngo.name} as pending.", "success")
                return redirect(url_for("admin_dashboard"))

        # also show all NGOs as fallback
        all_ngos = NGO.query.order_by(NGO.name.asc()).all()
        all_needs = NGONeed.query.filter_by(is_active=True).order_by(NGONeed.created_at.desc()).all()

        return render_template(
            "admin_donation_detail.html",
            donation=donation,
            all_ngos=all_ngos,
            all_needs=all_needs,
            show_admin_header=True,
            hide_admin_navbar=True, 
        )

    @app.route("/admin/ngos")
    @login_required(role="admin")
    def admin_ngos_list():
        ngos = NGO.query.order_by(NGO.city.asc(), NGO.name.asc()).all()
        needs_map = build_needs_map(ngos)
        return render_template("admin_ngos_list.html", ngos=ngos, needs_map=needs_map,  show_admin_header=True, hide_admin_navbar=False)

    
    @app.route("/admin/ngos/<int:ngo_id>/needs", methods=["GET", "POST"])
    @login_required(role="admin")
    def admin_manage_ngo_needs(ngo_id):
        ngo = NGO.query.get_or_404(ngo_id)

        if request.method == "POST":
            item_name = request.form.get("item_name", "").strip()
            category = request.form.get("category", "").strip()
            condition_needed = request.form.get("condition_needed", "").strip()
            details = request.form.get("details", "").strip()

            qty_required_raw = request.form.get("qty_required", "0").strip()
            try:
                qty_required = int(qty_required_raw)
            except ValueError:
                qty_required = -1

            if not item_name:
                flash("Item name is required.", "danger")
                return redirect(url_for("admin_manage_ngo_needs", ngo_id=ngo_id))

            if qty_required < 1:
                flash("Required quantity must be a positive number.", "danger")
                return redirect(url_for("admin_manage_ngo_needs", ngo_id=ngo_id))

            need = NGONeed(
                ngo_id=ngo.id,
                item_name=item_name,
                category=category or None,
                condition_needed=condition_needed or None,
                details=details or None,
                qty_required=qty_required,
                qty_fulfilled=0,
                is_active=True
            )
            db.session.add(need)
            db.session.commit()

            flash("Need added successfully.", "success")
            return redirect(url_for("admin_manage_ngo_needs", ngo_id=ngo_id))

        needs = NGONeed.query.filter_by(ngo_id=ngo.id).order_by(NGONeed.created_at.desc()).all()
        return render_template("admin_ngo_needs.html", ngo=ngo, needs=needs)


    @app.route("/admin/needs/<int:need_id>/toggle", methods=["POST"])
    @login_required(role="admin")
    def admin_toggle_need(need_id):
        need = NGONeed.query.get_or_404(need_id)
        need.is_active = not need.is_active
        db.session.commit()
        flash("Need status updated.", "success")
        return redirect(url_for("admin_manage_ngo_needs", ngo_id=need.ngo_id))

    # ---------- Thank-you notes ----------

    @app.route("/ngo/donations/<int:donation_id>/thank-you", methods=["POST"])
    @login_required(role="ngo")
    def ngo_add_thank_you(donation_id):
        ngo = current_ngo_profile()
        donation = Donation.query.get_or_404(donation_id)

        if not ngo or donation.ngo_id != ngo.id:
            flash("You do not have permission.", "danger")
            return redirect(url_for("ngo_donations"))

        if donation.status != "received":
            flash("Thank-you notes can only be added to received donations.", "warning")
            return redirect(url_for("ngo_donations") + "?tab=received")

        note = sanitize(request.form.get("thank_you_note", ""), 500)
        if not note:
            flash("Please write a thank-you message.", "warning")
            return redirect(url_for("ngo_donations") + "?tab=received")

        donation.thank_you_note = note
        db.session.commit()
        flash("Thank-you note saved.", "success")
        return redirect(url_for("ngo_donations") + "?tab=received")

    # ---------- Donor rating ----------

    @app.route("/donations/<int:donation_id>/rate", methods=["POST"])
    @login_required(role="donor")
    def donor_rate_donation(donation_id):
        user = current_user()
        donation = Donation.query.get_or_404(donation_id)

        if donation.donor_id != user.id:
            flash("You do not have permission.", "danger")
            return redirect(url_for("donor_profile"))

        if donation.status != "received":
            flash("Only received donations can be rated.", "warning")
            return redirect(url_for("donor_profile"))

        if donation.donor_rating is not None:
            flash("You have already rated this donation.", "info")
            return redirect(url_for("donor_profile"))

        rating_raw = request.form.get("rating", "").strip()
        review     = sanitize(request.form.get("review", ""), 500)

        try:
            rating = int(rating_raw)
        except (ValueError, TypeError):
            rating = 0

        if not (1 <= rating <= 5):
            flash("Please select a rating between 1 and 5 stars.", "danger")
            return redirect(url_for("donor_profile"))

        donation.donor_rating = rating
        donation.donor_review = review or None
        db.session.commit()
        flash("Thank you for your feedback!", "success")
        return redirect(url_for("donor_profile"))

    return app



def _migrate_db(db):
    """
    Safely add any columns that were introduced after the initial schema was created.
    Uses 'ALTER TABLE … ADD COLUMN' which is a no-op if the column already exists
    (SQLite ignores duplicate-column errors when we catch them).
    """
    migrations = [
        ("ngos",      "max_active_donations", "INTEGER"),
        ("donations", "thank_you_note",        "TEXT"),
        ("donations", "donor_rating",          "INTEGER"),
        ("donations", "donor_review",          "TEXT"),
    ]
    with db.engine.connect() as conn:
        for table, column, col_type in migrations:
            try:
                conn.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                conn.commit()
            except Exception:
                pass   # column already exists — safe to ignore


def seed_default_admin():
    """Seed admin account from .env on first run."""
    admin_email    = os.environ.get("ADMIN_EMAIL", "admin@zariya.pk")
    admin_password = os.environ.get("ADMIN_PASSWORD", "Admin@123")

    existing = User.query.filter_by(email=admin_email).first()
    if not existing:
        admin = User(full_name="System Admin", email=admin_email, phone=None, zone=None, role="admin")
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        print(f"[Zariya] Admin account created: {admin_email}")
    else:
        print(f"[Zariya] Admin already exists: {admin_email}")

def seed_ngos_if_empty():
    """
    Seed real NGOs and their public offices across Pakistan.
    Category mapping is based on each NGO's public programs, appeals,
    or donation pages and is intentionally kept to this app's item categories.
    """
    ngos = [
        {
            "name": "Edhi Foundation - Mithadar",
            "city": "Karachi",
            "zone": "Mithadar",
            "address": "Sarafa Bazar, Boulton Market, Mithadar, Karachi",
            "contact_email": "info@edhi.org",
            "contact_phone": "+92-21-32413232",
            "accepted_categories": "Food,Clothes,Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "Saylani Welfare International Trust - Bahadurabad",
            "city": "Karachi",
            "zone": "Bahadurabad",
            "address": "A-25, Bahadurabad Chowrangi, Karachi",
            "contact_email": "info@saylaniwelfare.com",
            "contact_phone": "+92-21-111-729-526",
            "accepted_categories": "Food,Education,Medical,Clothes",
            "has_pickup": True,
        },
        {
            "name": "Chhipa Welfare Association - Gulshan-e-Iqbal",
            "city": "Karachi",
            "zone": "Gulshan-e-Iqbal",
            "accepted_categories": "Clothes,Food,Medical",
            "has_pickup": True,
        },
        {
            "name": "Aman Foundation - Korangi",
            "city": "Karachi",
            "zone": "Korangi",
            "accepted_categories": "Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "Alkhidmat Foundation Karachi",
            "city": "Karachi",
            "zone": "Quaideen Colony",
            "address": "504 Quaideen Colony, Near Islamia College, Karachi",
            "contact_email": "karachi@alkhidmat.org",
            "contact_phone": "+92-21-111-503-504",
            "accepted_categories": "Food,Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "The Citizens Foundation - Karachi Head Office",
            "city": "Karachi",
            "zone": "Korangi Industrial Area",
            "address": "Plot No. 20, Sector 14, Near Brookes Chowrangi, Korangi Industrial Area, Karachi",
            "contact_email": "info@tcf.org.pk",
            "contact_phone": "0800-00823",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "HANDS Pakistan - Saddar",
            "city": "Karachi",
            "zone": "Saddar",
            "accepted_categories": "Medical,Food,Clothes",
            "has_pickup": True,
        },
        {
            "name": "SIUT - Civil Lines",
            "city": "Karachi",
            "zone": "Civil Lines",
            "accepted_categories": "Medical",
            "has_pickup": True,
        },
        {
            "name": "LRBT Free Eye Hospital - Landhi",
            "city": "Karachi",
            "zone": "Landhi",
            "accepted_categories": "Medical",
            "has_pickup": True,
        },
        {
            "name": "JDC Welfare Organization - Gulistan-e-Johar",
            "city": "Karachi",
            "zone": "Gulistan-e-Johar",
            "accepted_categories": "Food,Clothes,Medical",
            "has_pickup": True,
        },
        {
            "name": "Karachi Down Syndrome Program - PECHS",
            "city": "Karachi",
            "zone": "PECHS",
            "accepted_categories": "Education,Medical",
            "has_pickup": True,
        },
        {
            "name": "Dar-ul-Sukun - Kashmir Road",
            "city": "Karachi",
            "zone": "Kashmir Road",
            "accepted_categories": "Clothes,Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "Lyari Community Development Project",
            "city": "Karachi",
            "zone": "Lyari",
            "accepted_categories": "Education,Clothes,Food",
            "has_pickup": True,
        },
        {
            "name": "Sindh Institute of Physical Medicine & Rehabilitation",
            "city": "Karachi",
            "zone": "Gulshan-e-Hadid",
            "accepted_categories": "Medical",
            "has_pickup": True,
        },
        {
            "name": "Memon Medical Institute Welfare",
            "city": "Karachi",
            "zone": "Safoora",
            "accepted_categories": "Medical",
            "has_pickup": True,
        },
        {
            "name": "Marie Stopes Society - Garden",
            "city": "Karachi",
            "zone": "Garden",
            "accepted_categories": "Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "Legal Aid Society - Shahrah-e-Faisal",
            "city": "Karachi",
            "zone": "Shahrah-e-Faisal",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "DOHS Welfare Trust - Malir Cantt",
            "city": "Karachi",
            "zone": "Malir Cantt",
            "accepted_categories": "Food,Clothes",
            "has_pickup": True,
        },
        {
            "name": "Patients' Aid Foundation - JPMC",
            "city": "Karachi",
            "zone": "JPMC",
            "accepted_categories": "Medical",
            "has_pickup": True,
        },
        {
            "name": "Anjuman-e-Behbood-e-Samaji Gulberg",
            "city": "Karachi",
            "zone": "Gulberg",
            "accepted_categories": "Clothes,Food,Education",
            "has_pickup": True,
        },
        {
            "name": "Akhuwat Foundation - Township",
            "city": "Lahore",
            "zone": "Township",
            "address": "19 Civic Center, Sector A2, Township, Lahore",
            "contact_email": "info@akhuwat.org.pk",
            "contact_phone": "042-111-448-464",
            "accepted_categories": "Clothes,Food,Education,Medical",
            "has_pickup": True,
        },
        {
            "name": "Shauoor Welfare Foundation - Lahore",
            "city": "Lahore",
            "zone": "Sheikh Abdul Qadir Jilani Road",
            "address": "House 2 A, Gitto Street, Near UVAS, Sheikh Abdul Qadir Jilani Road, Lahore",
            "contact_email": "info@shauoor.org.pk",
            "contact_phone": "+92-42-37237470",
            "accepted_categories": "Clothes,Furniture,Education,Electronics",
            "has_pickup": True,
        },
        {
            "name": "Pakistan Citizens Alliance - Johar Town",
            "city": "Lahore",
            "zone": "Johar Town",
            "address": "831 F2 Johar Town, Lahore",
            "contact_email": "adeel@pca.org.pk",
            "contact_phone": "+92-336-4962721",
            "accepted_categories": "Food,Clothes,Education,Medical",
            "has_pickup": True,
        },
        {
            "name": "Foundation for Information Technology and Educational Development - Lahore",
            "city": "Lahore",
            "zone": "Iqbal Avenue",
            "address": "8-B, Iqbal Avenue Phase 1, Nazaria-e-Pakistan Road, Lahore",
            "contact_email": "contact@fited.org.pk",
            "contact_phone": "+92-341-6977375",
            "accepted_categories": "Education,Electronics",
            "has_pickup": True,
        },
        {
            "name": "Alkhidmat Foundation Pakistan Head Office - Lahore",
            "city": "Lahore",
            "zone": "Khayaban-e-Jinnah",
            "address": "3 KM Khayaban-e-Jinnah, next to Water Filteration Plant, Dream Villas, Lahore",
            "contact_email": "info@alkhidmat.org",
            "contact_phone": "+92-42-38020222",
            "accepted_categories": "Food,Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "The Citizens Foundation - Lahore Regional Office",
            "city": "Lahore",
            "zone": "DHA Phase VIII",
            "address": "Plot 91, Block C, Broadway Road, Phase VIII, DHA Lahore",
            "contact_email": "umer.shahid@tcf.org.pk",
            "contact_phone": "+92-42-37135871",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "SOS Children's Village Lahore",
            "city": "Lahore",
            "zone": "Ferozepur Road",
            "address": "SOS Children's Village Lahore, Ferozepur Road, Lahore-54600",
            "contact_email": "lahore@sos.org.pk",
            "contact_phone": "+92-42-35943922",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "Shad Foundation - Islamabad",
            "city": "Islamabad",
            "zone": "G-10/4",
            "address": "H-1027, Service Road East, G-10/4, Islamabad",
            "contact_email": "info@shadfoundation.org.pk",
            "contact_phone": "+92-300-5010580",
            "accepted_categories": "Education,Medical",
            "has_pickup": True,
        },
        {
            "name": "Fatima Shafi Foundation - Islamabad",
            "city": "Islamabad",
            "zone": "Blue Area",
            "address": "38-East, Zahoor Plaza, D-Chowk, Jinnah Avenue, Blue Area, Islamabad",
            "contact_email": "contact@fsf.org.pk",
            "contact_phone": "+92-51-8449801",
            "accepted_categories": "Food,Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "SOS Children's Village Islamabad",
            "city": "Islamabad",
            "zone": "H-11",
            "address": "Near Police Academy, Opposite NUST University, H-11, Islamabad",
            "contact_email": "islamabad@sos.org.pk",
            "contact_phone": "+92-51-8918220",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "The Citizens Foundation - Rawalpindi Regional Office",
            "city": "Rawalpindi",
            "zone": "Gangal West",
            "address": "Regional Office near Fazia Colony, Service Road, Gangal West, Rawalpindi",
            "contact_email": "syeda.mizhgan@tcf.org.pk",
            "contact_phone": "+92-51-4578229",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "Alkhidmat Foundation - Rawalpindi",
            "city": "Rawalpindi",
            "zone": "Mareer Chowk",
            "address": "Plot 427, Alkhidmat Complex, Behind Jang Building, Mareer Chowk, Murree Road, Rawalpindi",
            "contact_email": "ajk@alkhidmat.org",
            "contact_phone": "+92-51-4906080",
            "accepted_categories": "Food,Medical,Education",
            "has_pickup": True,
        },
        {
            "name": "SOS Children's Village Rawalpindi",
            "city": "Rawalpindi",
            "zone": "G.T. Road",
            "address": "Opposite High Court, G.T. Road, Rawalpindi",
            "contact_email": "rawalpindi@sos.org.pk",
            "contact_phone": "+92-51-4917312",
            "accepted_categories": "Education",
            "has_pickup": True,
        },
        {
            "name": "SOS Children's Village Faisalabad",
            "city": "Faisalabad",
            "zone": "Gatwala",
            "address": "Chak No. 199/R.B, Gatwala Post Office Gatwala, Faisalabad",
            "contact_email": "faisalabad@sos.org.pk",
            "contact_phone": "+92-41-2421667",
            "accepted_categories": "Education",
            "has_pickup": False,
        },
        {
            "name": "SOS Children's Village Multan",
            "city": "Multan",
            "zone": "Industrial Estate",
            "address": "Industrial Estate, Sector No. 1, Multan",
            "contact_email": "multan@sos.org.pk",
            "contact_phone": "+92-61-6538481",
            "accepted_categories": "Education",
            "has_pickup": False,
        },
        {
            "name": "SOS Children's Village Peshawar",
            "city": "Peshawar",
            "zone": "Hayatabad",
            "address": "Beside Shaukat Khanum Hospital, 5-A-2 Main Boulevard, Phase V, Hayatabad, Peshawar",
            "contact_email": "peshawar@sos.org.pk",
            "contact_phone": "+92-91-5812776",
            "accepted_categories": "Education",
            "has_pickup": False,
        },
    ]

    legacy_name_map = {
        "Edhi Foundation - Karachi (Mithadar)": "Edhi Foundation - Mithadar",
        "Saylani Welfare Trust - Bahadurabad": "Saylani Welfare International Trust - Bahadurabad",
        "Chhipa Welfare Association - Gulshan": "Chhipa Welfare Association - Gulshan-e-Iqbal",
        "Aman Foundation - Korangi": "Aman Foundation - Korangi",
        "Alkhidmat Foundation - North Karachi": "Alkhidmat Foundation Karachi",
        "The Citizens Foundation - Clifton": "The Citizens Foundation - Karachi Head Office",
        "HANDS Pakistan - Saddar": "HANDS Pakistan - Saddar",
        "SIUT - Civil Lines": "SIUT - Civil Lines",
        "LRBT Free Eye Hospital - Landhi": "LRBT Free Eye Hospital - Landhi",
        "JDC Welfare Organization - Johar": "JDC Welfare Organization - Gulistan-e-Johar",
        "Karachi Down Syndrome Program - PECHS": "Karachi Down Syndrome Program - PECHS",
        "Dar-ul-Sukun - Kashmir Road": "Dar-ul-Sukun - Kashmir Road",
        "Lyari Community Development Project": "Lyari Community Development Project",
        "Sindh Institute of Physical Medicine & Rehabilitation": "Sindh Institute of Physical Medicine & Rehabilitation",
        "Memon Medical Institute Welfare": "Memon Medical Institute Welfare",
        "Marie Stopes Society - Garden": "Marie Stopes Society - Garden",
        "Legal Aid Society - Shahrah-e-Faisal": "Legal Aid Society - Shahrah-e-Faisal",
        "DOHS Welfare Trust - Malir Cantt": "DOHS Welfare Trust - Malir Cantt",
        "Patients' Aid Foundation - JPMC": "Patients' Aid Foundation - JPMC",
        "Anjuman-e-Behbood-e-Samaji Gulberg": "Anjuman-e-Behbood-e-Samaji Gulberg",
    }

    existing_by_name = {ngo.name: ngo for ngo in NGO.query.all()}

    for payload in ngos:
        ngo = existing_by_name.get(payload["name"])
        legacy_name = next(
            (old_name for old_name, new_name in legacy_name_map.items() if new_name == payload["name"]),
            None,
        )

        if not ngo and legacy_name:
            ngo = existing_by_name.get(legacy_name)

        # If both old and new rows exist, move children to the new row and remove the duplicate old row.
        if ngo and legacy_name and ngo.name == payload["name"] and legacy_name in existing_by_name:
            legacy_ngo = existing_by_name[legacy_name]
            if legacy_ngo.id != ngo.id:
                Donation.query.filter_by(ngo_id=legacy_ngo.id).update({"ngo_id": ngo.id})
                NGONeed.query.filter_by(ngo_id=legacy_ngo.id).update({"ngo_id": ngo.id})
                db.session.delete(legacy_ngo)
                db.session.flush()

        if not ngo:
            ngo = NGO(name=payload["name"], is_verified=True)
            db.session.add(ngo)

        ngo.name = payload["name"]
        ngo.city = payload["city"]
        ngo.zone = payload.get("zone")
        ngo.address = payload.get("address")
        ngo.contact_email = payload.get("contact_email")
        ngo.contact_phone = payload.get("contact_phone")
        ngo.accepted_categories = payload.get("accepted_categories")
        ngo.has_pickup = True
        ngo.is_verified = True

        existing_by_name[ngo.name] = ngo

    db.session.commit()

if __name__ == "__main__":
    app = create_app()
    app.run(debug=True)

