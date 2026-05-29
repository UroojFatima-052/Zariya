from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db


class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True)
    full_name     = db.Column(db.String(120), nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    phone         = db.Column(db.String(30), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role          = db.Column(db.String(20), default="donor")   # donor | ngo | admin
    zone          = db.Column(db.String(50), nullable=True)     # donor's area/zone

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    donations = db.relationship("Donation", backref="donor", lazy=True)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class NGO(db.Model):
    __tablename__ = "ngos"

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(200), nullable=False)
    city             = db.Column(db.String(100), nullable=False, default="Karachi")
    zone             = db.Column(db.String(50), nullable=True)
    address          = db.Column(db.String(255), nullable=True)
    description      = db.Column(db.Text, nullable=True)          # About the NGO
    contact_email    = db.Column(db.String(120), nullable=True)
    contact_phone    = db.Column(db.String(50), nullable=True)

    accepted_categories  = db.Column(db.String(255), nullable=True)  # comma-separated
    is_verified          = db.Column(db.Boolean, default=True)
    has_pickup           = db.Column(db.Boolean, default=True)       # NGOs always pick up
    max_active_donations = db.Column(db.Integer, nullable=True)      # None = unlimited

    donations = db.relationship("Donation", backref="ngo", lazy=True)


class NGOApplication(db.Model):
    __tablename__ = "ngo_applications"

    id                  = db.Column(db.Integer, primary_key=True)
    organization_name   = db.Column(db.String(200), nullable=False)
    representative_name = db.Column(db.String(120), nullable=False)
    email               = db.Column(db.String(120), unique=True, nullable=False)
    phone               = db.Column(db.String(50), nullable=False)
    password_hash       = db.Column(db.String(255), nullable=False)

    city               = db.Column(db.String(100), nullable=False)
    zone               = db.Column(db.String(50), nullable=True)
    address            = db.Column(db.String(255), nullable=True)
    accepted_categories = db.Column(db.String(255), nullable=True)
    details            = db.Column(db.Text, nullable=True)

    status       = db.Column(db.String(20), default="pending")  # pending | approved | rejected
    admin_notes  = db.Column(db.Text, nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at  = db.Column(db.DateTime, nullable=True)

    approved_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    approved_ngo_id  = db.Column(db.Integer, db.ForeignKey("ngos.id"), nullable=True)

    approved_user = db.relationship("User", foreign_keys=[approved_user_id], lazy=True)
    approved_ngo  = db.relationship("NGO",  foreign_keys=[approved_ngo_id],  lazy=True)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class NGONeed(db.Model):
    __tablename__ = "ngo_needs"

    id       = db.Column(db.Integer, primary_key=True)
    ngo_id   = db.Column(db.Integer, db.ForeignKey("ngos.id"), nullable=False)

    item_name        = db.Column(db.String(200), nullable=False)
    category         = db.Column(db.String(100), nullable=True)
    details          = db.Column(db.Text, nullable=True)
    condition_needed = db.Column(db.String(50), nullable=True)   # New | Used | Any

    qty_required  = db.Column(db.Integer, nullable=False, default=0)
    qty_fulfilled = db.Column(db.Integer, nullable=False, default=0)

    is_active  = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ngo = db.relationship("NGO", backref=db.backref("needs", lazy=True))

    @property
    def qty_remaining(self):
        return max(0, (self.qty_required or 0) - (self.qty_fulfilled or 0))


class Donation(db.Model):
    __tablename__ = "donations"

    id          = db.Column(db.Integer, primary_key=True)
    tracking_id = db.Column(db.String(20), unique=True, nullable=False)

    # Item details
    item_name       = db.Column(db.String(200), nullable=False)
    category_manual = db.Column(db.String(100), nullable=True)
    quantity        = db.Column(db.Integer, nullable=True)
    condition       = db.Column(db.String(50), nullable=True)    # New | Used | Good
    description     = db.Column(db.Text, nullable=True)

    # Pickup info — donor provides where NGO can collect from
    pickup_address = db.Column(db.String(300), nullable=True)
    pickup_notes   = db.Column(db.Text, nullable=True)           # e.g. "Call before coming"

    # Status flow: pending → accepted → received
    #              pending → rejected
    #              pending → cancelled  (by donor)
    status = db.Column(db.String(20), default="pending")

    # Post-pickup feedback
    thank_you_note = db.Column(db.Text, nullable=True)    # NGO adds after received
    donor_rating   = db.Column(db.Integer, nullable=True) # 1–5, donor adds after received
    donor_review   = db.Column(db.Text, nullable=True)    # optional text from donor

    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    accepted_at   = db.Column(db.DateTime, nullable=True)   # when NGO accepted
    received_at   = db.Column(db.DateTime, nullable=True)   # when NGO marked as received
    cancelled_at  = db.Column(db.DateTime, nullable=True)   # when donor cancelled
    rejected_at   = db.Column(db.DateTime, nullable=True)
    rejected_reason = db.Column(db.Text, nullable=True)

    donor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    ngo_id   = db.Column(db.Integer, db.ForeignKey("ngos.id"), nullable=True)
    need_id  = db.Column(db.Integer, db.ForeignKey("ngo_needs.id"), nullable=True)
    need     = db.relationship("NGONeed", backref="donations", lazy=True)


class Complaint(db.Model):
    __tablename__ = "complaints"

    id          = db.Column(db.Integer, primary_key=True)
    user_id     = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    donation_id = db.Column(db.Integer, db.ForeignKey("donations.id"), nullable=True)

    subject     = db.Column(db.String(200), nullable=False)
    message     = db.Column(db.Text, nullable=False)
    status      = db.Column(db.String(20), default="open")   # open | resolved
    admin_notes = db.Column(db.Text, nullable=True)

    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    resolved_at = db.Column(db.DateTime, nullable=True)

    user     = db.relationship("User",     backref=db.backref("complaints", lazy=True))
    donation = db.relationship("Donation", backref=db.backref("complaints", lazy=True))


class DonationDraft(db.Model):
    __tablename__ = "donation_drafts"

    id      = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    item_name  = db.Column(db.String(200), nullable=True)
    category   = db.Column(db.String(100), nullable=True)
    quantity   = db.Column(db.Integer, nullable=True)
    condition  = db.Column(db.String(50), nullable=True)
    description = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("drafts", lazy=True))


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    token      = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used       = db.Column(db.Boolean, default=False)

    user = db.relationship("User", backref=db.backref("reset_tokens", lazy=True))


class DonorAlert(db.Model):
    __tablename__ = "donor_alerts"

    id        = db.Column(db.Integer, primary_key=True)
    user_id   = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    item_name = db.Column(db.String(200), nullable=False)
    category  = db.Column(db.String(100), nullable=True)
    quantity  = db.Column(db.Integer, nullable=False, default=1)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", backref=db.backref("alerts", lazy=True))
