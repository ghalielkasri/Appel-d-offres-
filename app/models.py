"""Modele de donnees : avis de consultation, attributions, profils de veille, journaux."""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TenderStatus(str, enum.Enum):
    OPEN = "open"            # consultation en cours
    CLOSED = "closed"        # date limite passee, resultat inconnu
    AWARDED = "awarded"      # attribue
    CANCELLED = "cancelled"  # annule / infructueux


class Category(str, enum.Enum):
    TRAVAUX = "Travaux"
    FOURNITURES = "Fournitures"
    SERVICES = "Services"
    AUTRE = "Autre"


class Buyer(Base):
    """Acheteur public (ministere, commune, etablissement...)."""

    __tablename__ = "buyers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    acronym: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tenders: Mapped[list["Tender"]] = relationship(back_populates="buyer")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Buyer {self.name!r}>"


class Supplier(Base):
    """Attributaire : l'entreprise qui a remporte un marche."""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(512))
    normalized_name: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    awards: Mapped[list["Award"]] = relationship(back_populates="supplier")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Supplier {self.name!r}>"


class Tender(Base):
    """Avis de consultation (appel d'offres, concours, bon de commande)."""

    __tablename__ = "tenders"
    __table_args__ = (
        UniqueConstraint("source", "source_key", name="uq_tender_source_key"),
        Index("ix_tender_publication", "publication_date"),
        Index("ix_tender_deadline", "submission_deadline"),
        Index("ix_tender_status_score", "status", "match_score"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- Provenance ---
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str] = mapped_column(String(256))
    reference: Mapped[str | None] = mapped_column(String(256), index=True)
    consultation_url: Mapped[str | None] = mapped_column(Text)

    # --- Contenu ---
    object: Mapped[str | None] = mapped_column(Text)
    category: Mapped[Category] = mapped_column(
        Enum(Category, native_enum=False, length=32), default=Category.AUTRE
    )
    procedure_type: Mapped[str | None] = mapped_column(String(256))
    execution_place: Mapped[str | None] = mapped_column(String(512))
    lots_count: Mapped[int | None] = mapped_column(Integer)

    # --- Acheteur ---
    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("buyers.id"), index=True)
    buyer_raw: Mapped[str | None] = mapped_column(String(512))
    buyer_service: Mapped[str | None] = mapped_column(String(512))

    # --- Dates ---
    publication_date: Mapped[date | None] = mapped_column(Date)
    submission_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opening_session_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Montants ---
    estimated_amount: Mapped[float | None] = mapped_column(Float)
    caution_provisoire: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="MAD")

    # --- Veille ---
    status: Mapped[TenderStatus] = mapped_column(
        Enum(TenderStatus, native_enum=False, length=32), default=TenderStatus.OPEN, index=True
    )
    match_score: Mapped[float] = mapped_column(Float, default=0.0)
    matched_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    matched_profiles: Mapped[list[int]] = mapped_column(JSON, default=list)

    # --- Recherche ---
    # Forme sans accents et en minuscules de l'objet, de l'acheteur, du lieu et de
    # la reference. Le portail publie "materiel" avec accent ; sans cette colonne,
    # une recherche tapee sans accent ne trouverait rien.
    search_text: Mapped[str | None] = mapped_column(Text, index=True)

    # Familles d'equipement reconnues dans l'objet (voir app/taxonomy.py).
    # C'est ce qui permet de rapprocher deux marches portant sur le meme type
    # de materiel : "froid", "cuisson", "laverie"...
    families: Mapped[list[str]] = mapped_column(JSON, default=list)

    # --- Tracabilite : la charge brute est conservee pour tout re-exploiter plus tard ---
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    buyer: Mapped[Buyer | None] = relationship(back_populates="tenders")
    awards: Mapped[list["Award"]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )
    notifications: Mapped[list["NotificationLog"]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )
    documents: Mapped[list["TenderDocument"]] = relationship(
        back_populates="tender", cascade="all, delete-orphan"
    )

    @property
    def days_left(self) -> int | None:
        """Jours restants avant la date limite, negatif si elle est passee."""
        if not self.submission_deadline:
            return None
        deadline = self.submission_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (deadline - datetime.now(timezone.utc)).days

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tender {self.reference!r} {self.status.value}>"


class Award(Base):
    """Resultat d'attribution : qui a gagne, pour combien, face a combien de concurrents."""

    __tablename__ = "awards"
    __table_args__ = (
        UniqueConstraint("source", "source_key", name="uq_award_source_key"),
        Index("ix_award_date", "award_date"),
        Index("ix_award_supplier_amount", "supplier_id", "awarded_amount"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[str] = mapped_column(String(64), index=True)
    source_key: Mapped[str] = mapped_column(String(256))

    # Rattachement a l'avis d'origine quand la reference permet de le retrouver.
    tender_id: Mapped[int | None] = mapped_column(ForeignKey("tenders.id"), index=True)
    reference: Mapped[str | None] = mapped_column(String(256), index=True)
    object: Mapped[str | None] = mapped_column(Text)
    lot_number: Mapped[str | None] = mapped_column(String(64))

    buyer_id: Mapped[int | None] = mapped_column(ForeignKey("buyers.id"), index=True)
    buyer_raw: Mapped[str | None] = mapped_column(String(512))

    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), index=True)
    supplier_raw: Mapped[str | None] = mapped_column(String(512))

    awarded_amount: Mapped[float | None] = mapped_column(Float)
    estimated_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8), default="MAD")
    amount_includes_tax: Mapped[bool | None] = mapped_column(Boolean)
    bids_count: Mapped[int | None] = mapped_column(Integer)

    award_date: Mapped[date | None] = mapped_column(Date)
    category: Mapped[Category] = mapped_column(
        Enum(Category, native_enum=False, length=32), default=Category.AUTRE
    )
    procedure_type: Mapped[str | None] = mapped_column(String(256))
    consultation_url: Mapped[str | None] = mapped_column(Text)

    # Forme sans accents et en minuscules : voir Tender.search_text.
    search_text: Mapped[str | None] = mapped_column(Text, index=True)
    # Familles d'equipement : voir Tender.families.
    families: Mapped[list[str]] = mapped_column(JSON, default=list)

    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tender: Mapped[Tender | None] = relationship(back_populates="awards")
    supplier: Mapped[Supplier | None] = relationship(back_populates="awards")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Award {self.reference!r} -> {self.supplier_raw!r} {self.awarded_amount}>"


class DocumentKind(str, enum.Enum):
    """Pieces du dossier de consultation (DCE)."""

    CPS = "cps"                  # cahier des prescriptions speciales
    BORDEREAU = "bordereau"      # bordereau des prix / BPU / DQE
    REGLEMENT = "reglement"      # reglement de consultation
    AVIS = "avis"                # avis d'appel d'offres
    PLAN = "plan"
    CPT = "cpt"                  # cahier des prescriptions techniques
    AUTRE = "autre"


class DocumentStatus(str, enum.Enum):
    LISTED = "listed"                  # reference connue, fichier pas encore recupere
    DOWNLOADED = "downloaded"
    AUTH_REQUIRED = "auth_required"    # le portail exige un compte entreprise
    FAILED = "failed"


class TenderDocument(Base):
    """Une piece du DCE : CPS, bordereau des prix, reglement, plan...

    Le portail n'ouvre le telechargement du DCE qu'aux comptes entreprise
    (Espace fournisseur). Une reference peut donc etre connue sans que le
    fichier soit recuperable : c'est le role du statut.
    """

    __tablename__ = "tender_documents"
    __table_args__ = (
        UniqueConstraint("tender_id", "url", name="uq_document_tender_url"),
        Index("ix_document_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id"), index=True)

    kind: Mapped[DocumentKind] = mapped_column(
        Enum(DocumentKind, native_enum=False, length=32), default=DocumentKind.AUTRE
    )
    name: Mapped[str | None] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(Text)

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, native_enum=False, length=32), default=DocumentStatus.LISTED
    )
    local_path: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)

    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)

    tender: Mapped[Tender] = relationship(back_populates="documents")

    @property
    def kind_label(self) -> str:
        return {
            DocumentKind.CPS: "CPS",
            DocumentKind.BORDEREAU: "Bordereau des prix",
            DocumentKind.REGLEMENT: "Reglement de consultation",
            DocumentKind.AVIS: "Avis d'appel d'offres",
            DocumentKind.PLAN: "Plans",
            DocumentKind.CPT: "Prescriptions techniques",
            DocumentKind.AUTRE: "Autre piece",
        }[self.kind]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<TenderDocument {self.kind.value} {self.name!r} {self.status.value}>"


class WatchProfile(Base):
    """Profil de veille : les mots-cles et filtres qui definissent votre domaine."""

    __tablename__ = "watch_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Mots-cles recherches dans l'objet, l'acheteur et le type de procedure.
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Mots-cles qui disqualifient un avis meme s'il correspond par ailleurs.
    excluded_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Categories retenues ("Fournitures", "Travaux", "Services") ; vide = toutes.
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Restriction optionnelle a certains acheteurs.
    buyers: Mapped[list[str]] = mapped_column(JSON, default=list)

    min_amount: Mapped[float | None] = mapped_column(Float)
    max_amount: Mapped[float | None] = mapped_column(Float)
    # Score minimal pour declencher une notification.
    min_score: Mapped[float] = mapped_column(Float, default=1.0)
    # Canaux a utiliser ; vide = tous ceux qui sont configures.
    channels: Mapped[list[str]] = mapped_column(JSON, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<WatchProfile {self.name!r} active={self.active}>"


class NotificationLog(Base):
    """Journal des envois : garantit qu'un avis n'est jamais notifie deux fois."""

    __tablename__ = "notification_logs"
    __table_args__ = (
        UniqueConstraint(
            "tender_id", "profile_id", "channel", "target", name="uq_notification_once"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tender_id: Mapped[int] = mapped_column(ForeignKey("tenders.id"), index=True)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("watch_profiles.id"))
    channel: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), default="sent")
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tender: Mapped[Tender] = relationship(back_populates="notifications")


class SourceRun(Base):
    """Trace de chaque collecte : volumes, duree, cout estime, erreurs."""

    __tablename__ = "source_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="running")
    items_fetched: Mapped[int] = mapped_column(Integer, default=0)
    items_new: Mapped[int] = mapped_column(Integer, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, default=0)
    notifications_sent: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SourceRun {self.source} {self.status} fetched={self.items_fetched}>"
