# Zariya — Product Requirements Document
> Last updated: 2026-03-18

---

## 1. What Is Zariya?

Zariya is a **donation middleware platform** — it sits between people who want to donate physical items and NGOs that need those items.

The closest real-world analogy is **Daraz**:

| Daraz | Zariya |
|-------|--------|
| Sellers list products | NGOs list their needs |
| Buyers browse & purchase | Donors browse & choose what to donate |
| Seller ships to buyer | NGO comes to pick up from donor |
| Order history & tracking | Donation history & tracking |
| Seller dashboard | NGO dashboard |
| Buyer profile | Donor profile |

Zariya does **not** handle money. It routes **physical items only** (clothes, food, books, medicine, furniture, electronics).

---

## 2. The Three Users

### Donor
A regular person who has items to give away. They register, browse NGO needs, pick one, and offer to donate. The NGO comes to them for pickup.

### NGO
A verified charity or welfare organization. They apply to join, get approved by Admin, then manage their own profile and post what items they need. When a donor offers something, they accept or decline and arrange pickup.

### Admin (Zariya team)
Internal staff. They approve/reject NGO applications, handle complaints, monitor the platform, and can intervene in any donation.

---

## 3. Complete User Flows

### 3.1 Donor Flow

```
1. Register
   → Full name, email, phone, password, city, zone/area

2. Browse
   → Search NGOs by name, city, or category
   → OR search active needs ("books", "clothes", "medicine")
   → Filter by: City | Category | Has Pickup | Verified only

3. View NGO Profile
   → See NGO description, city/zone, accepted categories
   → See all their active needs (like product listings)
   → See stats: total donations received, fulfilled needs

4. Pick a Need → Click "Donate This"
   → Fill donation form:
      - Item name & description
      - Quantity
      - Condition (New / Good / Worn)
      - Pickup address (their home/office)
      - Available days/times for pickup
      - Optional photo (future)

5. Donation submitted
   → Status: Pending NGO Response
   → Donor gets a Tracking ID

6. NGO Accepts
   → Status: Accepted — Awaiting Pickup
   → NGO contacts donor (phone number revealed to NGO)
   → Pickup is arranged

7. NGO picks up the item
   → NGO marks: "Picked Up"
   → Status: Completed
   → Donor sees it in their history

8. If something goes wrong:
   → Donor can File a Complaint
   → e.g. "NGO never showed up", "NGO was rude"
```

### 3.2 NGO Flow

```
1. Apply to Join
   → Organization name, type (welfare/education/medical etc.)
   → Representative name, CNIC, designation
   → Registration number (govt. registration if available)
   → Contact email, phone, WhatsApp
   → City, full address, zone
   → What categories they accept
   → Brief description of their work
   → Password

2. Admin reviews → Approved or Rejected
   → On approval: NGO account is activated

3. NGO logs in → Dashboard
   → Stats: Active needs, incoming donations, completed donations
   → Manage needs, manage incoming donations

4. Post a Need (like listing a product on Daraz)
   → Item name
   → Category
   → Quantity needed
   → Condition accepted (New / Any / Good only)
   → Notes/details
   → Need stays active until fulfilled or manually closed

5. Receive Donation Offer
   → Notification (on-platform, email in future)
   → See donor's details: name, area, pickup address, available times
   → Accept or Decline (must give reason if declining)

6. After Accepting
   → Donor's phone number is revealed
   → NGO calls to confirm pickup time
   → NGO dispatches volunteer/vehicle

7. After Pickup
   → NGO marks donation as "Picked Up"
   → Optional: Add a thank-you note to the donor

8. If item is not as described on arrival:
   → NGO can mark "Rejected on Arrival" with reason
   → Goes to Admin as a complaint/dispute
```

### 3.3 Admin Flow

```
1. Login → Admin Dashboard
   → Pending NGO applications
   → Active complaints
   → Platform stats (donations today, this week, by city)

2. Review NGO Application
   → See all submitted info
   → Approve (activates NGO account) or Reject (with reason)

3. Manage Complaints
   → See complaint details (who filed, against whom, description)
   → Investigate: view donation timeline
   → Resolve: warn NGO / suspend NGO / dismiss complaint

4. Manage NGOs
   → View all NGOs, their status, load
   → Suspend or unsuspend an NGO
   → Edit NGO details if needed

5. Manage Donations
   → View all donations by status
   → Intervene: manually reassign a donation if NGO ghosted
   → Reject a donation with reason

6. Analytics
   → Donations by city, category, month
   → Most active NGOs
   → Fulfillment rates
```

---

## 4. Donation Statuses (Complete Lifecycle)

```
pending_ngo          → Submitted by donor, waiting for NGO to accept
accepted             → NGO accepted, pickup being arranged
picked_up            → NGO physically collected the item  ✅ DONE
rejected_by_ngo      → NGO declined (donor can re-offer to another NGO)
rejected_on_arrival  → NGO arrived but item wasn't acceptable
cancelled_by_donor   → Donor withdrew before pickup
rejected_by_admin    → Admin removed the donation (policy violation)
```

---

## 5. Complaint System

### Who can file a complaint:
- **Donor against NGO** — NGO never showed up, NGO was rude, item taken but not marked, etc.
- **NGO against Donor** — Donor gave wrong address, item completely different from description, abusive behavior.

### Complaint form fields:
- Who is the complaint against (auto-filled from donation)
- Linked donation (tracking ID)
- Category: No-show | Misbehavior | Item mismatch | Fraud | Other
- Description (free text)
- Submitted at (timestamp)

### Admin resolves:
- Dismiss (no action)
- Warn (internal flag on account)
- Suspend account (temporary)
- Ban account (permanent, for fraud)

---

## 6. NGO Profile Page (Public)

Visible to all visitors, no login needed.

- NGO name, logo placeholder, verified badge
- City, zone, address
- Short description of their work
- Categories they accept
- Active needs (like product listings — scrollable cards)
- Stats: Total donations received, total needs fulfilled
- "Donate to this NGO" button

---

## 7. Search & Browse

### Search bar (homepage):
- Search by NGO name
- Search by item/need (e.g. "books", "wheelchair")

### Filters:
- City (Karachi, Lahore, Islamabad, etc.)
- Category (Food, Clothes, Medical, Education, Electronics, Furniture)
- Has active needs only
- Verified NGOs only

### Results:
- NGO cards: logo, name, city, categories, active needs count, verified badge
- Need cards: item name, NGO name, quantity needed, condition, city

---

## 8. Development Phases

### Phase 1 — Core That Works (MVP)
Fix the broken foundation so the basic flow works end-to-end.

- [ ] Remove the auto-matching engine (replace with donor-chooses model)
- [ ] Fix seeded NGOs visibility bug (critical blocker)
- [ ] Add pickup address + available times to donation form
- [ ] Add "Accept / Decline" for NGO with reason on decline
- [ ] Add "Mark as Picked Up" for NGO
- [ ] Add complete donation status lifecycle
- [ ] Fix login_required decorator bug
- [ ] Fix NGO current_load never decreasing
- [ ] Fix tracking ID race condition
- [ ] Add run.py entry point
- [ ] Move hardcoded admin password to .env

### Phase 2 — Make It Feel Like Daraz (UI Rebuild)
Rebuild the entire frontend with Tailwind CSS.

- [ ] Integrate Tailwind CSS (CDN for demo, npm for production)
- [ ] Redesign homepage with hero, search bar, featured NGOs
- [ ] Redesign NGO browse/search page with filter sidebar + cards
- [ ] Redesign NGO public profile page
- [ ] Redesign donor registration, login pages
- [ ] Redesign donor dashboard (stats + history + active donations)
- [ ] Redesign NGO dashboard (sidebar layout, needs management)
- [ ] Redesign admin dashboard
- [ ] Make everything mobile responsive

### Phase 3 — Security & Completeness
- [ ] Complaint system (file, view, admin resolve)
- [ ] Password reset via email (Flask-Mail + Gmail SMTP)
- [ ] Email notifications (donation accepted, pickup confirmed, etc.)
- [ ] NGO profile edit (by NGO themselves)
- [ ] Pagination on all list pages
- [ ] Input sanitization audit
- [ ] Rate limiting on login (prevent brute force)
- [ ] .env file for all secrets

### Phase 4 — Polish & Launch Ready
- [ ] Donor can re-offer a declined donation to a different NGO
- [ ] NGO can add thank-you note after pickup
- [ ] Donor can rate/review the pickup experience
- [ ] Admin analytics with charts (Chart.js)
- [ ] City-based filtering for routing (Lahore NGO can't pick up from Karachi)
- [ ] NGO capacity limit (max active accepted donations at one time)
- [ ] SEO basics (meta tags, page titles)

---

## 9. Future Features (Post-Launch)

### 9.1 Fundraiser Campaigns (inspired by JustGiving)
NGOs can create a public fundraiser campaign:
- Campaign name & story ("Help us collect 500 winter blankets for flood victims")
- Target: item + quantity (not money)
- End date
- Progress bar showing how many donated so far
- Shareable link (donors can share on WhatsApp etc.)
- Campaign page shows all donors who contributed (with permission)

This turns Zariya from a passive directory into an **active campaign platform**.

### 9.2 NGO Verification Tiers
- **Basic** — applied and approved by admin
- **Verified** — submitted govt. registration documents
- **Platinum** — track record of 100+ completed donations on platform

### 9.3 Donor Badges & Gamification
- "First Donation" badge
- "Regular Donor" (5+ donations)
- "Community Hero" (20+ donations)
- Leaderboard by city (anonymous, opt-in)

### 9.4 Volunteer Module
NGOs can post volunteer requests (not just item needs).
Donors can sign up to volunteer instead of or in addition to donating items.

### 9.5 Corporate Donations
Companies can register as "Corporate Donors" with bulk donation capabilities and get a CSR report at year end.

### 9.6 In-Platform Chat (Donor ↔ NGO)
Once a donation is accepted, a private chat thread opens between the donor and the NGO — tied to that specific donation.

**Why it matters:** Right now the only way for them to communicate is a phone call. Chat keeps everything on-platform (better trust, evidence trail for complaints, no need to share phone numbers upfront).

**How it works:**
- Chat is only unlocked after NGO accepts a donation (not before — prevents spam)
- Each donation has its own chat thread
- Messages are tied to the donation record — Admin can view them when resolving a complaint
- Chat closes automatically once donation is marked "Picked Up" (read-only after that)
- Unread message badge on dashboard for both donor and NGO

**What they'd use it for:**
- Confirming pickup time and date
- Sharing exact address / directions
- "I'll be home after 5pm" / "Our van is arriving at 3pm"
- Sending a thank-you message after pickup

**Tech options (in order of complexity):**
1. **Simple polling** — browser checks for new messages every 5 seconds (easiest, good for demo)
2. **Flask-SocketIO** — real WebSocket-based chat (proper real-time, moderate effort)
3. **Third-party** — Pusher or Firebase Realtime DB (easiest real-time, but external dependency)

For demo: start with polling. For production: Flask-SocketIO.

### 9.7 Real-time Notifications
WebSocket or SSE-based live notifications inside the app.

### 9.7 Mobile App
React Native or Flutter app using the same Flask backend as an API.

---

## 10. Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python / Flask |
| Database | SQLite (demo) → PostgreSQL (production) |
| ORM | Flask-SQLAlchemy |
| Frontend | Jinja2 templates + Tailwind CSS |
| Charts | Chart.js |
| Email | Flask-Mail + Gmail SMTP |
| Auth | Session-based (current) |
| Hosting (future) | Railway / Render (free tier) |

---

## 11. Design System

### Colors
| Role | Hex |
|------|-----|
| Primary (brand green) | `#1a6b3c` |
| Primary light | `#e8f5ee` |
| Accent (warm orange) | `#e8620a` |
| Text primary | `#111827` |
| Text secondary | `#6b7280` |
| Border | `#e5e7eb` |
| Background | `#f9fafb` |
| White | `#ffffff` |
| Danger | `#dc2626` |
| Success | `#16a34a` |
| Warning | `#d97706` |

### Typography
- Font: **Inter** (Google Fonts)
- Headings: `font-bold`, sizes: 36px / 28px / 22px / 18px
- Body: `font-normal`, 15px
- Small/meta: 13px, `text-secondary`

### Component Patterns
- Cards: `rounded-xl shadow-sm border border-gray-100 bg-white p-5`
- Buttons primary: `bg-[#1a6b3c] text-white rounded-lg px-5 py-2.5 font-medium hover:bg-[#155530]`
- Buttons outline: `border border-[#1a6b3c] text-[#1a6b3c] rounded-lg px-5 py-2.5`
- Badges: `rounded-full px-3 py-1 text-xs font-medium`
- Inputs: `border border-gray-300 rounded-lg px-4 py-2.5 w-full focus:ring-2 focus:ring-[#1a6b3c]`

---

*This document is the single source of truth for the Zariya project.*
*Update it as decisions change. Do not build anything not in this document.*
