"""Demo-data catalogue and the deterministic seed plan.

Pure data + pure functions: this module performs no I/O at all, so the entire shape of a
seeding run can be inspected with `--dry-run` before a single request is sent.

The plan is generated BEFORE the first HTTP call. That ordering is what makes the run
reproducible: `--random-seed` fully determines which incidents exist, who submits them, how
far they travel through the workflow and what timestamps the backfill pass will write. Two
runs with the same seed produce byte-identical plans.

Two facts from the domain constrain everything here and are not negotiable:

* Only `staff` may submit an incident, only `reviewer`/`tenant_admin` may transition it,
  and only `tenant_admin` may reassign — to another *active reviewer of the same tenant*
  (app/api/v1/routes/incidents.py, app/services/workflow_service.py). Every actor named in
  a plan step is therefore chosen from the role that is actually allowed to perform it.
* The only legal edges are `open -> in_review -> closed`, and closing requires a note
  (UC-08 E2). A plan never contains a step the state machine would reject.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.enums import IncidentStatus, Role, Severity

# ---- Identity ---------------------------------------------------------------------

# Plus-aliases of a real mailbox. GoTrue's built-in SMTP rejects undeliverable domains, and
# `POST /api/v1/organizations` cannot avoid sending one invite per organisation (see the
# module docstring of seed_demo.py), so the admin addresses MUST be deliverable.
MAILBOX_LOCAL = "bazhenov.in"
MAILBOX_DOMAIN = "gmail.com"
# Every seeded address starts with this tag. Nothing outside it is ever touched by --purge.
EMAIL_TAG = "fd-demo"


def demo_email(org_slug: str, user_slug: str) -> str:
    """Build the plus-aliased address for one seeded user."""
    return f"{MAILBOX_LOCAL}+{EMAIL_TAG}-{org_slug}-{user_slug}@{MAILBOX_DOMAIN}"


# ---- Specs ------------------------------------------------------------------------


@dataclass(frozen=True)
class UserSpec:
    slug: str
    name: str
    role: Role

    def email(self, org_slug: str) -> str:
        return demo_email(org_slug, self.slug)


@dataclass(frozen=True)
class CategorySpec:
    name: str
    description: str


@dataclass(frozen=True)
class OrgSpec:
    slug: str
    name: str
    users: tuple[UserSpec, ...]
    categories: tuple[CategorySpec, ...]
    #: Weekly incident volume shape. Multiplied by the base rate; length is irrelevant,
    #: the series is cycled/interpolated across the window so each org trends differently.
    volume_shape: tuple[float, ...]

    @property
    def admin(self) -> UserSpec:
        return next(u for u in self.users if u.role is Role.tenant_admin)

    @property
    def staff(self) -> tuple[UserSpec, ...]:
        return tuple(u for u in self.users if u.role is Role.staff)

    @property
    def reviewers(self) -> tuple[UserSpec, ...]:
        return tuple(u for u in self.users if u.role is Role.reviewer)


# ---- The catalogue ----------------------------------------------------------------

_SHARED_CATEGORIES: tuple[CategorySpec, ...] = (
    CategorySpec("Network", "Connectivity, VPN, Wi-Fi and link outages."),
    CategorySpec("Hardware", "Laptops, peripherals, printers and on-site equipment."),
    CategorySpec("Software", "Line-of-business applications, licensing and updates."),
    CategorySpec("Access & Identity", "Accounts, permissions, SSO and password resets."),
    CategorySpec("Facilities", "Building services, power, HVAC and physical workspace."),
    CategorySpec("Security", "Suspected compromise, phishing and policy violations."),
)

ORGS: tuple[OrgSpec, ...] = (
    OrgSpec(
        slug="north",
        name="FlowDesk Demo — Northbridge Health",
        users=(
            UserSpec("admin", "Alina Novak", Role.tenant_admin),
            UserSpec("staff1", "Daniel Okafor", Role.staff),
            UserSpec("staff2", "Priya Raman", Role.staff),
            UserSpec("rev1", "Marcus Feld", Role.reviewer),
            UserSpec("rev2", "Sofia Lindqvist", Role.reviewer),
        ),
        categories=_SHARED_CATEGORIES,
        # Rising trend: a growing organisation logging more each month.
        volume_shape=(0.6, 0.7, 0.8, 0.9, 1.0, 1.0, 1.1, 1.2, 1.3, 1.3, 1.4, 1.5),
    ),
    OrgSpec(
        slug="river",
        name="FlowDesk Demo — Riverton Logistics",
        users=(
            UserSpec("admin", "Tomas Berger", Role.tenant_admin),
            UserSpec("staff1", "Hannah Wu", Role.staff),
            UserSpec("staff2", "Owen Delacroix", Role.staff),
            UserSpec("rev1", "Yusuf Karim", Role.reviewer),
            UserSpec("rev2", "Elena Costa", Role.reviewer),
        ),
        categories=_SHARED_CATEGORIES,
        # Spiky: a mid-window incident surge, then recovery. Makes the two org charts
        # visibly different rather than two copies of the same curve.
        volume_shape=(1.0, 0.9, 1.1, 1.8, 2.0, 1.6, 0.9, 0.8, 1.0, 1.1, 0.9, 1.0),
    ),
)

# ---- Incident text ----------------------------------------------------------------

# (title, description) pairs per category name. Drawn without replacement per org where
# possible, so a 40-incident org reads like a real queue rather than one line repeated.
_INCIDENT_TEXT: dict[str, tuple[tuple[str, str], ...]] = {
    "Network": (
        (
            "Site-to-site VPN dropping every few minutes",
            "The tunnel to the secondary site renegotiates roughly every four minutes. "
            "Users on shared drives lose their session mid-save. Started after the "
            "firmware update on the edge router.",
        ),
        (
            "Wi-Fi unusable in the east wing",
            "Signal shows full bars but throughput is under 1 Mbps. Wired ports in the "
            "same rooms are fine, so it looks like the access points rather than the "
            "uplink.",
        ),
        (
            "DNS resolution failing for internal hostnames",
            "External sites resolve normally, internal ones time out. Flushing the "
            "resolver cache helps for a few minutes, then it returns.",
        ),
        (
            "Intermittent packet loss to the cloud provider",
            "Roughly 8% loss on the path to ap-southeast-2 during business hours, clean "
            "overnight. Traceroute shows the loss starting at the third hop.",
        ),
        (
            "Guest network hands out expired leases",
            "Visitors connect but get an address outside the guest range and cannot reach "
            "the captive portal. Reproducible on any device.",
        ),
        (
            "Video calls degrade every afternoon",
            "Between roughly 2pm and 4pm calls freeze and audio robotises. Bandwidth "
            "graphs do not show saturation, so it may be a QoS policy.",
        ),
        (
            "Switch port flapping in the comms room",
            "Port 14 on the second-floor switch cycles up and down. The attached device "
            "is a wall-mounted display that keeps losing its schedule.",
        ),
    ),
    "Hardware": (
        (
            "Laptop will not charge past 40%",
            "Battery health reads normal but the charge stalls at 40% on both the "
            "original adapter and a spare. Unit is 14 months old and still in warranty.",
        ),
        (
            "Ward printer jams on every duplex job",
            "Single-sided printing is fine. Duplex jams in the same place each time; the "
            "rollers look worn. Staff are printing single-sided as a workaround.",
        ),
        (
            "Docking station drops external monitors",
            "Both monitors blank for two seconds roughly every ten minutes. Swapping "
            "cables made no difference; a different dock does not reproduce it.",
        ),
        (
            "Barcode scanner misreads batch labels",
            "About one scan in twenty returns the wrong last digit. Reprinting the label "
            "does not help, so it appears to be the scanner rather than print quality.",
        ),
        (
            "Desktop fails POST after power outage",
            "Machine powers on, fans spin, no display and no beep codes. Reseating RAM "
            "and clearing CMOS did not change the behaviour.",
        ),
        (
            "Meeting room display shows no signal over HDMI",
            "The same laptop and cable work in the adjacent room. Firmware on the display "
            "is two versions behind.",
        ),
        (
            "Handheld terminal battery drains within two hours",
            "Rated for a full shift. Two of the six units in the pool are affected; the "
            "rest hold charge normally.",
        ),
    ),
    "Software": (
        (
            "Rostering application crashes on export",
            "Exporting a roster longer than four weeks closes the application with no "
            "error dialog. Shorter ranges export fine.",
        ),
        (
            "Reporting module returns yesterday's figures",
            "The dashboard is a day behind for every metric. The overnight job reports "
            "success in its own log, so the data may be cached downstream.",
        ),
        (
            "Licence check fails after the version upgrade",
            "Users see a licence-expired banner even though the subscription renews next "
            "year. Rolling back the client removes the banner.",
        ),
        (
            "PDF generation produces blank pages",
            "Documents over roughly 30 pages come out with the last third blank. Smaller "
            "documents are unaffected.",
        ),
        (
            "Scheduled sync stops silently after an error",
            "Once a single record fails validation the whole job stops and never retries. "
            "Nothing appears in the notification channel.",
        ),
        (
            "Search returns no results for valid records",
            "Records open fine by direct link but do not appear in search. Suspect the "
            "index has not rebuilt since the migration.",
        ),
        (
            "Mobile app logs users out on every cold start",
            "The session token is not persisted, so staff re-authenticate several times a "
            "shift. Started with the last store release.",
        ),
    ),
    "Access & Identity": (
        (
            "New starter cannot access the shared drive",
            "Account was created and the group membership looks correct, but the mapped "
            "drive returns access denied. Other new starters this week are fine.",
        ),
        (
            "SSO redirect loop on the finance portal",
            "Sign-in bounces between the identity provider and the portal until the "
            "browser gives up. Clearing cookies fixes it for one session only.",
        ),
        (
            "Password reset emails never arrive",
            "The reset is accepted and logged, but no message is delivered. Not in junk. "
            "Affects external contractors only.",
        ),
        (
            "Contractor retains access after end date",
            "Account should have been disabled automatically. Still able to sign in and "
            "read project folders — raised as a compliance concern.",
        ),
        (
            "MFA prompt not appearing on managed devices",
            "Sign-in completes without the second factor on corporate laptops. Personal "
            "devices are prompted correctly.",
        ),
        (
            "Shared mailbox permissions reset overnight",
            "Delegated access is removed each night and has to be re-granted every "
            "morning. Suspect a provisioning job is overwriting it.",
        ),
        (
            "Role change did not propagate to the application",
            "User was moved to a supervisor group two days ago but the application still "
            "shows the old permission set.",
        ),
    ),
    "Facilities": (
        (
            "Air conditioning failed in the server room",
            "Ambient temperature is climbing past 31C. Portable units are in place as a "
            "stopgap. Escalated because equipment is at risk.",
        ),
        (
            "Badge reader on the loading dock unresponsive",
            "No light, no beep. Staff are being let in manually, which defeats the access "
            "log.",
        ),
        (
            "Emergency lighting test failed on level 2",
            "Three fittings did not illuminate during the monthly test. Logged for the "
            "compliance record as well as repair.",
        ),
        (
            "Water ingress near the second-floor comms cupboard",
            "Damp patch on the ceiling tile directly above the patch panel. No visible "
            "drip yet, but it is spreading.",
        ),
        (
            "Automatic doors stay open in high wind",
            "The sensor retriggers continuously, so the doors never close. Heating cost "
            "and security both affected.",
        ),
        (
            "Desk power outlets dead in the open-plan area",
            "One full bank of desks has no power at floor level. Overhead lighting on the "
            "same circuit is unaffected.",
        ),
        (
            "Lift alarm sounding intermittently with no fault shown",
            "Alarm triggers with an empty car. The service panel reports no fault, so a "
            "contractor visit is needed.",
        ),
    ),
    "Security": (
        (
            "Phishing campaign targeting payroll staff",
            "Several near-identical messages impersonating the finance director, asking "
            "for bank detail changes. At least one recipient opened the attachment.",
        ),
        (
            "Unusual sign-in from an unrecognised location",
            "Successful authentication from outside the country twelve minutes after a "
            "local sign-in by the same account. Credentials assumed compromised.",
        ),
        (
            "Antivirus quarantined a file on a clinical workstation",
            "Detection on a scheduled scan. The workstation has been isolated pending "
            "review of what the file was doing there.",
        ),
        (
            "USB device policy bypassed on a shared terminal",
            "An unapproved storage device mounted successfully and files were copied. "
            "Policy should have blocked it.",
        ),
        (
            "Public S3-style bucket found with internal documents",
            "A storage container used for a pilot is readable without authentication. "
            "Contents include internal process documents.",
        ),
        (
            "Repeated failed logins against the VPN gateway",
            "Several thousand attempts across many usernames over three hours from a "
            "small address range. No success observed so far.",
        ),
        (
            "Expired TLS certificate on an internal service",
            "Users are being trained to click through the browser warning, which is the "
            "real problem. Certificate lapsed four days ago.",
        ),
    ),
}

_RESOLUTION_NOTES: tuple[str, ...] = (
    "Root cause identified and corrected; verified with the reporter before closing.",
    "Faulty component replaced from stock. Monitored for 24 hours with no recurrence.",
    "Configuration reverted to the last known-good state. Change request raised for the "
    "permanent fix.",
    "Vendor patch applied and validated in the affected environment.",
    "Access corrected and the underlying group membership fixed at source.",
    "Duplicate of an earlier report; resolved together with the parent incident.",
    "Workaround documented in the knowledge base and accepted by the requester.",
    "Confirmed as expected behaviour after review with the service owner. No change "
    "required.",
)

# A note is optional on `open -> in_review` (it is only mandatory when closing, UC-08 E2),
# so the pool includes None to exercise both shapes of the timeline in the UI.
_REVIEW_NOTES: tuple[str | None, ...] = (
    "Picked up for triage. Reproduced on a second device.",
    "Investigating — awaiting logs from the reporter.",
    "Assigned to the on-call engineer for this category.",
    "Confirmed and prioritised against this week's queue.",
    None,
    None,
)

# ---- Plan shape -------------------------------------------------------------------

#: How many whole ISO weeks the generated data spans. Matches analytics_service.DEFAULT_WEEKS
#: so the default `/analytics/volume` window is fully populated end to end.
WINDOW_WEEKS = 12

#: Target incidents per organisation, before the per-week shaping is applied.
INCIDENTS_PER_ORG = 40

#: Final-status mix. Weighted towards `open` for recent weeks and `closed` for old ones —
#: see `_pick_status`, which tilts these by the incident's age.
_SEVERITY_WEIGHTS: tuple[tuple[Severity, float], ...] = (
    (Severity.low, 0.30),
    (Severity.medium, 0.35),
    (Severity.high, 0.25),
    (Severity.critical, 0.10),
)

#: Share of non-open incidents that also get an explicit tenant-admin reassignment
#: (UC-08 A1). Kept small: it is there to prove the path works, not to dominate the data.
_REASSIGN_SHARE = 0.12


@dataclass
class IncidentPlan:
    """One incident and the exact sequence of API calls that will produce it."""

    org_slug: str
    category_name: str
    submitter_slug: str
    title: str
    description: str
    severity: Severity
    final_status: IncidentStatus

    #: Timestamps the backfill pass will write. `created_at` is what the API cannot set.
    created_at: datetime
    #: reviewer who performs open -> in_review (None when the incident stays open)
    reviewer_slug: str | None = None
    review_at: datetime | None = None
    review_note: str | None = None
    #: tenant-admin reassignment to the OTHER reviewer (UC-08 A1), optional
    reassign_to_slug: str | None = None
    reassign_at: datetime | None = None
    #: reviewer who performs in_review -> closed (always the currently assigned one)
    closer_slug: str | None = None
    closed_at: datetime | None = None
    close_note: str | None = None

    @property
    def updated_at(self) -> datetime:
        """Latest write to the incident row — what `incidents.updated_at` must become."""
        return max(
            t
            for t in (self.created_at, self.review_at, self.reassign_at, self.closed_at)
            if t is not None
        )

    @property
    def transition_times(self) -> list[datetime]:
        """Transition timestamps in write order (matches `transitions[]` from the API)."""
        return [t for t in (self.review_at, self.closed_at) if t is not None]

    @property
    def event_times(self) -> list[datetime]:
        """Every notification-producing event, in write order.

        A reassignment writes no `workflow_transitions` row (it is not a state change,
        see app/models/workflow_transition.py) but it DOES produce a notification, so the
        notification backfill keys off this list rather than `transition_times`.
        """
        return [
            t
            for t in (self.review_at, self.reassign_at, self.closed_at)
            if t is not None
        ]


@dataclass
class OrgPlan:
    spec: OrgSpec
    incidents: list[IncidentPlan] = field(default_factory=list)

    @property
    def status_counts(self) -> dict[IncidentStatus, int]:
        counts = {state: 0 for state in IncidentStatus}
        for inc in self.incidents:
            counts[inc.final_status] += 1
        return counts


# ---- Generation -------------------------------------------------------------------


def week_start(day: date) -> date:
    """Monday on or before `day`.

    Deliberately identical to `analytics_service.week_start` (ISO, Monday-based) rather
    than imported: if that definition ever drifts from PostgreSQL's `date_trunc('week')`,
    the seed data should not drift silently with it.
    """
    return day - timedelta(days=day.weekday())


def _weighted(rng: random.Random, choices: tuple[tuple[object, float], ...]):
    population = [c for c, _ in choices]
    weights = [w for _, w in choices]
    return rng.choices(population, weights=weights, k=1)[0]


def _pick_status(rng: random.Random, age_fraction: float) -> IncidentStatus:
    """Final status, tilted by age.

    `age_fraction` is 0.0 for the oldest week in the window and 1.0 for the newest. Old
    incidents have had time to be worked and are mostly closed; this week's are mostly
    still open. Without the tilt the status mix is uniform across the window and the demo
    looks synthetic.
    """
    closed_w = 0.62 * (1.0 - age_fraction) ** 1.5 + 0.03
    review_w = 0.30 + 0.18 * (1.0 - abs(age_fraction - 0.45) * 2)
    open_w = 0.15 + 0.75 * age_fraction**1.3
    return _weighted(
        rng,
        (
            (IncidentStatus.open, open_w),
            (IncidentStatus.in_review, max(review_w, 0.05)),
            (IncidentStatus.closed, max(closed_w, 0.0)),
        ),
    )  # type: ignore[return-value]


def _business_datetime(rng: random.Random, day: date, tz: ZoneInfo) -> datetime:
    """A plausible submission time: weekdays, 07:45–18:30, minute resolution."""
    hour = rng.choices(
        population=[8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
        weights=[6, 12, 14, 12, 7, 9, 12, 11, 9, 5, 3],
        k=1,
    )[0]
    return datetime(
        day.year, day.month, day.day, hour, rng.randrange(0, 60), rng.randrange(0, 60),
        tzinfo=tz,
    )


def _weekly_counts(
    rng: random.Random, shape: tuple[float, ...], total: int, weeks: int
) -> list[int]:
    """Distribute `total` incidents across `weeks`, following `shape`, exactly."""
    # Stretch/compress the shape onto the window, then normalise to `total`.
    stretched = [shape[round(i * (len(shape) - 1) / max(weeks - 1, 1))] for i in range(weeks)]
    jittered = [max(v * rng.uniform(0.75, 1.25), 0.05) for v in stretched]
    scale = total / sum(jittered)
    counts = [int(v * scale) for v in jittered]
    # Hand out the rounding remainder to the heaviest weeks, so the sum is exact.
    remainder = total - sum(counts)
    order = sorted(range(weeks), key=lambda i: jittered[i], reverse=True)
    for i in range(remainder):
        counts[order[i % weeks]] += 1
    return counts


def build_plan(
    *,
    seed: int,
    now: datetime,
    tz: ZoneInfo,
    orgs: tuple[OrgSpec, ...] = ORGS,
    incidents_per_org: int = INCIDENTS_PER_ORG,
    weeks: int = WINDOW_WEEKS,
) -> list[OrgPlan]:
    """Build the full, deterministic seeding plan.

    `now` must already be in `tz` — it is the ceiling for every generated timestamp, so no
    plan can ever describe an incident from the future.
    """
    rng = random.Random(seed)
    plans: list[OrgPlan] = []
    # The window ends with the CURRENT week, so `/analytics/volume`'s default (which ends
    # at today) has data in its final bucket rather than a trailing empty bar.
    last_week = week_start(now.date())
    first_week = last_week - timedelta(weeks=weeks - 1)

    for spec in orgs:
        plan = OrgPlan(spec=spec)
        counts = _weekly_counts(rng, spec.volume_shape, incidents_per_org, weeks)
        # Text is drawn without replacement per (org, category) so titles stay distinct.
        pools = {c.name: list(_INCIDENT_TEXT[c.name]) for c in spec.categories}
        for pool in pools.values():
            rng.shuffle(pool)
        cursors = {name: 0 for name in pools}

        for week_index, count in enumerate(counts):
            monday = first_week + timedelta(weeks=week_index)
            age_fraction = week_index / max(weeks - 1, 1)
            for _ in range(count):
                category = rng.choice(spec.categories)
                pool = pools[category.name]
                title, description = pool[cursors[category.name] % len(pool)]
                cursors[category.name] += 1

                # Weekdays only, and never past `now` for the current week.
                day = monday + timedelta(days=rng.choices(range(5), [5, 5, 5, 4, 3])[0])
                created = _business_datetime(rng, day, tz)
                if created >= now:
                    created = now - timedelta(minutes=rng.randrange(20, 600))

                status = _pick_status(rng, age_fraction)
                incident = IncidentPlan(
                    org_slug=spec.slug,
                    category_name=category.name,
                    submitter_slug=rng.choice(spec.staff).slug,
                    title=title,
                    description=description,
                    severity=_weighted(rng, _SEVERITY_WEIGHTS),  # type: ignore[arg-type]
                    final_status=status,
                    created_at=created,
                )
                _fill_workflow(rng, incident, spec, now)
                plan.incidents.append(incident)

        plan.incidents.sort(key=lambda i: i.created_at)
        plans.append(plan)
    return plans


def _fill_workflow(
    rng: random.Random, incident: IncidentPlan, spec: OrgSpec, now: datetime
) -> None:
    """Attach the transition/reassignment steps implied by `final_status`.

    Every timestamp is strictly increasing and clamped below `now`. If a clamp would
    collapse two steps onto the same instant the incident is demoted to the earlier state
    — a plan that cannot be executed truthfully is not worth executing.
    """
    if incident.final_status is IncidentStatus.open:
        return

    reviewer, other = rng.sample(list(spec.reviewers), k=2)
    review_at = incident.created_at + timedelta(
        hours=rng.choices([1, 3, 8, 20, 44], [4, 6, 5, 3, 2])[0],
        minutes=rng.randrange(0, 60),
    )
    if review_at >= now:
        incident.final_status = IncidentStatus.open
        return
    incident.reviewer_slug = reviewer.slug
    incident.review_at = review_at
    incident.review_note = rng.choice(_REVIEW_NOTES)

    assignee = reviewer
    if rng.random() < _REASSIGN_SHARE:
        reassign_at = review_at + timedelta(
            hours=rng.randrange(1, 30), minutes=rng.randrange(0, 60)
        )
        if reassign_at < now:
            incident.reassign_to_slug = other.slug
            incident.reassign_at = reassign_at
            assignee = other

    if incident.final_status is not IncidentStatus.closed:
        return

    base = incident.reassign_at or review_at
    closed_at = base + timedelta(
        hours=rng.choices([4, 12, 30, 70, 130], [4, 6, 5, 3, 2])[0],
        minutes=rng.randrange(0, 60),
    )
    if closed_at >= now:
        # Cannot close it in the past without lying about the timeline; leave it in review.
        incident.final_status = IncidentStatus.in_review
        return
    incident.closer_slug = assignee.slug
    incident.closed_at = closed_at
    incident.close_note = rng.choice(_RESOLUTION_NOTES)


# ---- Purge targets ----------------------------------------------------------------


def demo_tenant_names(orgs: tuple[OrgSpec, ...] = ORGS) -> list[str]:
    """Exact tenant names the seeder owns. `--purge` matches on these, never a wildcard."""
    return [o.name for o in orgs]


def demo_emails(orgs: tuple[OrgSpec, ...] = ORGS) -> list[str]:
    """Exact addresses the seeder owns, in Supabase Auth and in `users`."""
    return [u.email(o.slug) for o in orgs for u in o.users]
