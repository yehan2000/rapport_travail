# -*- coding: utf-8 -*-
"""
BTI – Rapport de travail (Pro, clean)
- Auth bcrypt + première connexion @b-t-i.ch (pending)
- Planning du jour = récap du jour + saisie (travail/absence)
- Mon mois = répartition par commune/domaine + détails + "doit" (admin-only)
- Résumé & Export = bilans + PDF (portrait + paysage) + Excel
- Dashboard compact
- Admin = employés, fériés, communes, domaines, budgets, verrouillage mois
- Fériés VD/fédéraux pris en compte dans le "doit" (jours ouvrés - fériés)
"""

import os, io, math
from typing import Optional
from datetime import datetime, date, time, timedelta
from calendar import monthrange
from sqlalchemy.exc import IntegrityError
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt
from reportlab.platypus import (
    SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether, Image  # + Image
)
from sqlalchemy import (
    create_engine, Column, Integer, String, Date, Time, Text, Boolean,
    ForeignKey, Float, UniqueConstraint, DateTime, inspect
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm


from reportlab.pdfbase.pdfmetrics import stringWidth

try:
    import bcrypt
except Exception:
    bcrypt = None

# -----------------------------
# Config
# -----------------------------
APP_TITLE = "BTI – Rapport de travail"
DAILY_WORK_HOURS_DEFAULT = 8.5  # heures/jour ouvré
DB_DIR = ".data"
os.makedirs(DB_DIR, exist_ok=True)
DB_URL = f"sqlite:///{os.path.join(DB_DIR, 'bti.db')}"
engine = create_engine(DB_URL, echo=False, future=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()

# -----------------------------
# Models
# -----------------------------
class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    display_name = Column(String, unique=True, nullable=False)
    initials = Column(String, nullable=True)
    role = Column(String, default="user")
    active = Column(Boolean, default=True)
    email = Column(String, unique=True, nullable=True)
    pending = Column(Boolean, default=False)
    hashed_password = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    time_entries = relationship("TimeEntry", back_populates="employee")

# --- Nouveau modèle Débours ---
class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)
    fournisseur = Column(String, nullable=True)
    commune_id = Column(Integer, ForeignKey("communes.id"), nullable=True)  # "Intercommunal" si tu veux, c'est une commune aussi
    amount = Column(Float, nullable=False, default=0.0)  # CHF
    note = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class Commune(Base):
    __tablename__ = "communes"
    id = Column(Integer, primary_key=True)
    nom = Column(String, unique=True, nullable=False)
    time_entries = relationship("TimeEntry", back_populates="commune")

class Domaine(Base):
    __tablename__ = "domaines"
    id = Column(Integer, primary_key=True)
    libelle = Column(String, unique=True, nullable=False)
    time_entries = relationship("TimeEntry", back_populates="domaine")

class TimeEntry(Base):
    __tablename__ = "time_entries"
    id = Column(Integer, primary_key=True)

    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    date = Column(Date, nullable=False)

    heure_debut = Column(Time, nullable=True)
    heure_fin = Column(Time, nullable=True)
    duree_min = Column(Integer, nullable=True)

    type = Column(String, default="travail")  # travail|absence
    absence_type = Column(String, nullable=True)  # vacances|congés statutaires|maladie|armée-PC

    commune_id = Column(Integer, ForeignKey("communes.id"), nullable=True)
    domaine_id = Column(Integer, ForeignKey("domaines.id"), nullable=True)

    dossier = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    facturable = Column(Boolean, default=None)

    created_at = Column(DateTime, default=datetime.utcnow)

    employee = relationship("Employee", back_populates="time_entries")
    commune = relationship("Commune", back_populates="time_entries")
    domaine = relationship("Domaine", back_populates="time_entries")

    __table_args__ = (
        UniqueConstraint("employee_id", "date", "heure_debut", "heure_fin", "duree_min", "dossier", name="uq_entry_unique"),
    )

class TheoreticalHours(Base):
    __tablename__ = "theoretical_hours"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    hours = Column(Float, nullable=False)
    __table_args__ = (UniqueConstraint("employee_id", "year", "month", name="uq_theoretical"),)

class Budget(Base):
    __tablename__ = "budgets"
    id = Column(Integer, primary_key=True)
    commune_id = Column(Integer, ForeignKey("communes.id"), nullable=True)
    domaine_id = Column(Integer, ForeignKey("domaines.id"), nullable=True)
    year = Column(Integer, nullable=False)
    hours = Column(Float, nullable=True)
    __table_args__ = (UniqueConstraint("commune_id", "domaine_id", "year", name="uq_budget_unique"),)

class MonthLock(Base):
    __tablename__ = "month_locks"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    locked = Column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("employee_id", "year", "month", name="uq_month_lock"),)

class Holiday(Base):
    __tablename__ = "holidays"
    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False, unique=True)
    label = Column(String, nullable=False)
    canton = Column(String, nullable=True)  # "VD" ou None pour fédéral

Base.metadata.create_all(engine)

# -----------------------------
# Gentle migrations
# -----------------------------
def ensure_columns():
    insp = inspect(engine)
    with engine.begin() as conn:
        cols_emp = [c['name'] for c in insp.get_columns('employees')]
        if 'email' not in cols_emp:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN email VARCHAR")
        if 'pending' not in cols_emp:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN pending BOOLEAN")
        if 'hashed_password' not in cols_emp:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN hashed_password VARCHAR")
        if 'created_at' not in cols_emp:
            conn.exec_driver_sql("ALTER TABLE employees ADD COLUMN created_at DATETIME")

        cols_te = [c['name'] for c in insp.get_columns('time_entries')]
        if 'created_at' not in cols_te:
            conn.exec_driver_sql("ALTER TABLE time_entries ADD COLUMN created_at DATETIME")

ensure_columns()

# -----------------------------
# Seed
# -----------------------------
def seed_if_empty():
    s = Session()
    try:
        if s.query(Employee).count() == 0:
            hpw = bcrypt.hashpw(b"admin", bcrypt.gensalt()).decode("utf-8") if bcrypt else None
            s.add(Employee(
            display_name="admin",
            initials="ADM",
            role="admin",
            email="admin@b-t-i.ch",
            pending=False,
            hashed_password=hpw,
            active=True
    ))
        if s.query(Commune).count() == 0:
            s.add_all([Commune(nom=n) for n in ["Corsier-sur-Vevey","Corseaux","Chardonne","Jongny","Intercommunal"]])
        if s.query(Domaine).count() == 0:
            s.add_all([Domaine(libelle=l) for l in ["Police des constructions","Aménagement du territoire","Assainissement","Gestion DP","SIT","Administration","Autre"]])
        s.commit()
    finally:
        s.close()

seed_if_empty()

# -----------------------------
# Utils
# -----------------------------
def debours_mensuels(session, employee: Employee, year: int, month: int):
    q = session.query(Expense, Commune).join(Commune, isouter=True).filter(
        Expense.employee_id == employee.id,
        Expense.date >= date(year, month, 1),
        Expense.date <= date(year, month, monthrange(year, month)[1])
    ).all()

    rows = []
    for e, c in q:
        rows.append({
            "Date": e.date.strftime("%d.%m.%Y"),
            "Fournisseur": e.fournisseur or "",
            "Commune": c.nom if c else "",
            "Montant": round(float(e.amount or 0.0), 2),
            "Note": e.note or "",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df, pd.DataFrame(), 0.0

    # Pivot style “large” (Dates / Fournisseurs / colonnes communes + Totaux)
    wide = df.pivot_table(index=["Date","Fournisseur"], columns="Commune", values="Montant", aggfunc="sum").fillna(0.0)
    wide["Totaux"] = wide.sum(axis=1)
    total = float(wide["Totaux"].sum())
    wide = wide.reset_index()
    return df, wide, total

def minutes_between(t1: time, t2: time) -> Optional[int]:
    if not t1 or not t2:
        return None
    dt1 = datetime.combine(date.today(), t1)
    dt2 = datetime.combine(date.today(), t2)
    if dt2 <= dt1:
        return None
    return int((dt2 - dt1).total_seconds() // 60)

def business_days_in_month(year: int, month: int) -> int:
    days = monthrange(year, month)[1]
    return sum(1 for d in range(1, days + 1) if date(year, month, d).weekday() < 5)

def business_days_with_holidays(session, year: int, month: int) -> int:
    total_bd = business_days_in_month(year, month)
    holis = session.query(Holiday).filter(
        Holiday.date >= date(year, month, 1),
        Holiday.date <= date(year, month, monthrange(year, month)[1])
    ).all()
    minus = sum(1 for h in holis if h.date.weekday() < 5)
    return max(total_bd - minus, 0)

def default_theoretical_hours(year: int, month: int, daily_hours: float = DAILY_WORK_HOURS_DEFAULT, session: Optional[Session] = None) -> float:
    if session is None:
        return business_days_in_month(year, month) * daily_hours
    bdw = business_days_with_holidays(session, year, month)
    return bdw * daily_hours

def month_name_fr(month: int) -> str:
    noms = ["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]
    return noms[month-1]

def fmt_hhmm_from_minutes(m: Optional[int | float]) -> str:
    if m is None or (isinstance(m, float) and math.isnan(m)):
        return ""
    m = int(round(m))
    h = m // 60
    mn = m % 60
    return f"{h}:{mn:02d}"

# -----------------------------
# Auth
# -----------------------------
def login_form():
    st.header("Connexion")
    tab_login, tab_signup = st.tabs(["Se connecter", "Première connexion"])

    with tab_login:
        name = st.text_input("Nom / Prénom (affichage)", key="login_name")
        pwd = st.text_input("Mot de passe", type="password", key="login_pwd")
        if st.button("Se connecter", key="login_btn"):
            s = Session()
            try:
                u = s.query(Employee).filter(Employee.display_name == name, Employee.active == True).first()
                if not u:
                    st.error("Utilisateur inconnu ou inactif")
                    return None
                if u.pending:
                    st.warning("Compte en attente d'approbation par un administrateur.")
                    return None
                if u.hashed_password and bcrypt:
                    ok = bcrypt.checkpw(pwd.encode("utf-8"), u.hashed_password.encode("utf-8"))
                else:
                    ok = (pwd == "admin")
                if ok:
                    st.session_state.user_id = u.id
                    st.session_state.user_role = u.role
                    st.success("Connecté ✅")
                    st.rerun()
                else:
                    st.error("Mot de passe incorrect")
            finally:
                s.close()

    with tab_signup:
        st.caption("Créez votre compte en utilisant votre adresse **@b-t-i.ch**. Un administrateur doit valider le compte.")
        full_name = st.text_input("Nom complet (affichage)", key="signup_fullname")
        initials = st.text_input("Initiales (ex: YV)", key="signup_initials")
        email = st.text_input("Email professionnel", placeholder="prenom.nom@b-t-i.ch", key="signup_email")
        pwd1 = st.text_input("Mot de passe", type="password", key="signup_pwd1")
        pwd2 = st.text_input("Confirmer le mot de passe", type="password", key="signup_pwd2")
        if st.button("Créer mon compte", key="signup_btn"):
            if not full_name or not email or not pwd1:
                st.error("Champs requis manquants.")
                return None
            if not email.lower().endswith("@b-t-i.ch"):
                st.error("Adresse email non autorisée. Utilisez votre email @b-t-i.ch")
                return None
            if pwd1 != pwd2:
                st.error("Les mots de passe ne correspondent pas.")
                return None
            s = Session()
            try:
                if s.query(Employee).filter((Employee.display_name == full_name) | (Employee.email == email)).first():
                    st.error("Un compte existe déjà avec ce nom ou cet email.")
                    return None
                hpw = bcrypt.hashpw(pwd1.encode("utf-8"), bcrypt.gensalt()).decode("utf-8") if bcrypt else None
                u = Employee(display_name=full_name, initials=(initials or None), email=email, hashed_password=hpw, role='user', active=True, pending=True)
                s.add(u); s.commit()
                st.success("Compte créé. En attente de validation par un administrateur.")
            finally:
                s.close()
    return None

def current_user(session) -> Optional[Employee]:
    uid = st.session_state.get("user_id")
    if not uid:
        return None
    return session.get(Employee, uid)

# -----------------------------
# PDF export (portrait + paysage)
# -----------------------------
def _footer(canvas, doc):
    canvas.saveState()
    page = canvas.getPageNumber()
    canvas.setFont("Helvetica", 9)
    canvas.setFillColor(colors.HexColor("#6B7280"))
    canvas.drawRightString(landscape(A4)[0]-doc.rightMargin, 1.0*cm, f"Page {page}")
    canvas.restoreState()

def _img_from_fig(fig, width_cm=10):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=160, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    img = Image(buf)
    img._restrictSize(width_cm*cm, 999*cm)
    return img

def build_pdf(
    employee_name: str,
    year: int,
    month: int,
    df_entries: pd.DataFrame,
    df_summary_commune: pd.DataFrame,
    df_summary_domaine: pd.DataFrame,
    df_summary_cross: pd.DataFrame,
    worked_minutes: int,
    absences: dict,
    theoretical_hours: float,
    extras: dict | None = None,
    # ↓↓↓ nouveaux paramètres pour débours ↓↓↓
    df_debours_wide: pd.DataFrame | None = None,
    debours_total: float = 0.0,
) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        leftMargin=1.2*cm, rightMargin=1.2*cm, topMargin=1.0*cm, bottomMargin=1.0*cm,
        title=f"Rapport {employee_name} {month:02d}/{year}", author="BTI",
    )

    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", fontSize=9, leading=11))
    styles["Heading1"].fontSize = 16; styles["Heading1"].spaceAfter = 6
    styles["Heading3"].fontSize = 12; styles["Heading3"].spaceBefore = 10; styles["Heading3"].spaceAfter = 4

    elems = []
    elems += [Paragraph("<b>Rapport de travail mensuel</b>", styles["Heading1"]),
              Paragraph(f"{employee_name} — {month_name_fr(month)} {year}", styles["Small"]),
              Spacer(1,6)]

    # KPI
    diff_m = (worked_minutes/60.0) - theoretical_hours
    kpi = Table([
        ["Heures théoriques", f"{theoretical_hours:.1f} h",
         "Heures effectuées", f"{worked_minutes/60.0:.1f} h",
         "Δ mois", f"{diff_m:+.1f} h"],
    ], colWidths=[4.3*cm, 3.0*cm, 5.0*cm, 3.0*cm, 2.2*cm, 2.8*cm])
    kpi.setStyle(TableStyle([
        ("FONT",(0,0),(-1,-1),"Helvetica",10),
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#EEF2FF")),
        ("BOX",(0,0),(-1,-1),0.25,colors.HexColor("#E5E7EB")),
    ]))
    elems += [kpi]

    if extras:
        ytd_diff = (extras["ytd_minutes"]/60.0) - extras["ytd_theoretical_hours"]
        ytd = Table([
            ["Effectuées (YTD)", f"{extras['ytd_minutes']/60.0:.1f} h",
             "Théoriques (YTD)", f"{extras['ytd_theoretical_hours']:.1f} h",
             "Δ année", f"{ytd_diff:+.1f} h"]
        ], colWidths=[4.3*cm, 3.0*cm, 5.0*cm, 3.0*cm, 2.2*cm, 2.8*cm])
        ytd.setStyle(TableStyle([
            ("FONT",(0,0),(-1,-1),"Helvetica",10),
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#F3F4F6")),
            ("BOX",(0,0),(-1,-1),0.25,colors.HexColor("#E5E7EB")),
        ]))
        elems += [Spacer(1,4), ytd]

    # Répartition % par commune
    elems += [Spacer(1,6), Paragraph("<b>Répartition par commune</b>", styles["Heading3"])]
    if not df_summary_commune.empty:
        tot = df_summary_commune["Minutes"].sum()
        dfc = df_summary_commune.copy()
        dfc["Part (%)"] = (dfc["Minutes"]/tot*100).round(1)
        tbl_c = Table([["Commune","Part (%)"]] + dfc[["Commune","Part (%)"]].values.tolist(),
                      colWidths=[10.0*cm, 4.0*cm])
        tbl_c.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E5E7EB")),
            ("FONT",(0,0),(-1,-1),"Helvetica",9),
            ("GRID",(0,0),(-1,-1),0.25,colors.grey),
            ("ALIGN",(1,1),(1,-1),"RIGHT"),
        ]))

        # Camembert
        fig, ax = plt.subplots(figsize=(4.5,4.5))
        ax.pie(dfc["Minutes"], labels=dfc["Commune"], autopct=lambda p: f"{p:.1f}%")
        ax.set_title("Part par commune")
        img_c = _img_from_fig(fig, width_cm=10)

        elems += [Table([[tbl_c, img_c]], colWidths=[14.5*cm, 10*cm], style=TableStyle([]))]
    else:
        elems += [Paragraph("—", styles["Small"])]

    # Répartition % par domaine
    elems += [Spacer(1,6), Paragraph("<b>Répartition par domaine</b>", styles["Heading3"])]
    if not df_summary_domaine.empty:
        tot = df_summary_domaine["Minutes"].sum()
        dfd = df_summary_domaine.copy()
        dfd["Part (%)"] = (dfd["Minutes"]/tot*100).round(1)
        tbl_d = Table([["Domaine","Part (%)"]] + dfd[["Domaine","Part (%)"]].values.tolist(),
                      colWidths=[10.0*cm, 4.0*cm])
        tbl_d.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E5E7EB")),
            ("FONT",(0,0),(-1,-1),"Helvetica",9),
            ("GRID",(0,0),(-1,-1),0.25,colors.grey),
            ("ALIGN",(1,1),(1,-1),"RIGHT"),
        ]))

        fig, ax = plt.subplots(figsize=(4.5,4.5))
        ax.pie(dfd["Minutes"], labels=dfd["Domaine"], autopct=lambda p: f"{p:.1f}%")
        ax.set_title("Part par domaine")
        img_d = _img_from_fig(fig, width_cm=10)

        elems += [Table([[tbl_d, img_d]], colWidths=[14.5*cm, 10*cm], style=TableStyle([]))]
    else:
        elems += [Paragraph("—", styles["Small"])]

    # Détails du mois (table compacte – on garde, mais sans "Notes")
    elems += [Spacer(1,6), Paragraph("<b>Feuille d'heures – détails</b>", styles["Heading3"])]
    if not df_entries.empty:
        cols = ["Date","Début","Fin","Durée","Commune","Domaine","Dossier"]
        data = [cols] + df_entries[cols].fillna("").values.tolist()
        total_w = landscape(A4)[0] - (doc.leftMargin + doc.rightMargin)
        cw = [2.2*cm, 1.5*cm, 1.5*cm, 1.7*cm, 6.0*cm, 7.0*cm, total_w - (2.2+1.5+1.5+1.7+6.0+7.0)*cm]
        t = Table(data, colWidths=cw, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E5E7EB")),
            ("GRID",(0,0),(-1,-1),0.25,colors.grey),
            ("FONT",(0,0),(-1,0),"Helvetica-Bold",9),
            ("FONT",(0,1),(-1,-1),"Helvetica",8.6),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
        ]))
        elems += [t]
    else:
        elems += [Paragraph("Aucune entrée ce mois.", styles["Small"])]

    # --- Débours du mois ---
    elems += [Spacer(1,6), Paragraph("<b>Débours du mois</b>", styles["Heading3"])]
    if df_debours_wide is not None and not df_debours_wide.empty:
        # Limiter la largeur, laisser ReportLab wrap si nécessaire
        cols = list(df_debours_wide.columns)
        data = [cols] + df_debours_wide.fillna("").values.tolist()
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#E5E7EB")),
            ("GRID",(0,0),(-1,-1),0.25,colors.grey),
            ("FONT",(0,0),(-1,-1),"Helvetica",8.6),
        ]))
        elems += [t, Spacer(1,4), Paragraph(f"<b>Total débours :</b> {debours_total:,.2f} CHF".replace(",", " ").replace(".", ","), styles["Small"])]
    else:
        elems += [Paragraph("—", styles["Small"])]

    doc.build(elems, onFirstPage=_footer, onLaterPages=_footer)
    pdf_bytes = buf.getvalue()
    buf.close()
    return pdf_bytes
# -----------------------------
# Data helpers
# -----------------------------
def compute_reports(session, employee: Employee, year: int, month: int):
    q = session.query(TimeEntry).filter(
        TimeEntry.employee_id == employee.id,
        TimeEntry.date >= date(year, month, 1),
        TimeEntry.date <= date(year, month, monthrange(year, month)[1])
    )
    entries = q.all()

    rows = []
    worked_minutes = 0
    absences = {"vacances":0, "congés statutaires":0, "maladie":0, "armée-PC":0}

    for e in entries:
        if e.type == "travail":
            worked_minutes += (e.duree_min or 0)
            rows.append({
                "Date": e.date.strftime('%d.%m.%Y'),
                "Début": e.heure_debut.strftime('%H:%M') if e.heure_debut else "",
                "Fin": e.heure_fin.strftime('%H:%M') if e.heure_fin else "",
                "Durée": fmt_hhmm_from_minutes(e.duree_min),
                "Minutes": e.duree_min or 0,
                "Commune": e.commune.nom if e.commune else "",
                "Domaine": e.domaine.libelle if e.domaine else "",
                "Dossier": e.dossier or "",
                "Note": e.description or "",
            })
        else:
            if e.absence_type in absences:
                absences[e.absence_type] += (e.duree_min or 0)

    df_entries = pd.DataFrame(rows)
    if not df_entries.empty:
        df_summary_commune = df_entries.groupby('Commune', dropna=False)['Minutes'].sum().reset_index()
        df_summary_domaine = df_entries.groupby('Domaine', dropna=False)['Minutes'].sum().reset_index()
        df_summary_cross = df_entries.groupby(['Commune','Domaine'], dropna=False)['Minutes'].sum().reset_index()
    else:
        df_summary_commune = pd.DataFrame(columns=['Commune','Minutes'])
        df_summary_domaine = pd.DataFrame(columns=['Domaine','Minutes'])
        df_summary_cross = pd.DataFrame(columns=['Commune','Domaine','Minutes'])

    th = session.query(TheoreticalHours).filter_by(employee_id=employee.id, year=year, month=month).first()
    theoretical = th.hours if th else default_theoretical_hours(year, month, session=session)

    # Cumuls année
    year_start = date(year, 1, 1)
    year_end = date(year, month, monthrange(year, month)[1])
    y_entries = session.query(TimeEntry).filter(
        TimeEntry.employee_id == employee.id,
        TimeEntry.date >= year_start,
        TimeEntry.date <= year_end
    ).all()
    y_worked = sum((e.duree_min or 0) for e in y_entries if e.type == 'travail')
    # heures théoriques cumulées Jan..month (avec fériés)
    y_theoretical = 0.0
    for m in range(1, month+1):
        thm = session.query(TheoreticalHours).filter_by(employee_id=employee.id, year=year, month=m).first()
        y_theoretical += (thm.hours if thm else default_theoretical_hours(year, m, session=session))
    y_vac = sum((e.duree_min or 0) for e in y_entries if e.type=='absence' and e.absence_type=='vacances')

    extras = {'ytd_minutes': y_worked, 'ytd_theoretical_hours': y_theoretical, 'ytd_vac_minutes': y_vac}
    return df_entries, df_summary_commune, df_summary_domaine, df_summary_cross, worked_minutes, absences, theoretical, extras

# -----------------------------
# Pages
# -----------------------------
def page_planning_jour(session, employee: Employee):
    st.subheader("Planning du jour")
    st.markdown('<div class="bti-band">Récap de la journée et saisie rapide</div>', unsafe_allow_html=True)

    today = date.today()

    # --- Récap du jour
    entries = session.query(TimeEntry).filter(
        TimeEntry.employee_id == employee.id,
        TimeEntry.date == today
    ).all()
    minutes_travail = sum((e.duree_min or 0) for e in entries if e.type == "travail")
    minutes_abs = sum((e.duree_min or 0) for e in entries if e.type == "absence")
    objectif_min = int(DAILY_WORK_HOURS_DEFAULT * 60)

    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("Réalisé aujourd'hui", fmt_hhmm_from_minutes(minutes_travail))
    with c2: st.metric("Objectif", fmt_hhmm_from_minutes(objectif_min))
    with c3: st.metric("Reste à faire", fmt_hhmm_from_minutes(max(objectif_min - minutes_travail, 0)))
    with c4: st.metric("Absences", fmt_hhmm_from_minutes(minutes_abs))
    st.progress(0 if objectif_min == 0 else min(minutes_travail / objectif_min, 1.0),
                text=f"{int((0 if objectif_min == 0 else min(minutes_travail / objectif_min, 1.0))*100)}% de l'objectif")

    # =========================
    #   Saisie du jour
    # =========================
    st.markdown("### Ajouter une entrée")

    # Choix hors-form pour dynamique immédiate
    typ = st.selectbox("Type", ["travail", "absence"], index=0, key="add_typ")

    # 👉 toggles ABSENCE aussi hors-form (pour que l'UI réagisse sans submit)
    if typ == "absence":
        st.markdown("**Paramètres d'absence**")
        abs_multi = st.checkbox("Plusieurs jours (plage)",
                        value=st.session_state.get("abs_multi_toggle", False),
                        key="abs_multi_toggle")

        abs_full = st.checkbox("Journée entière",
                       value=st.session_state.get("abs_full_toggle", True),
                       key="abs_full_toggle")


    with st.form("add_entry_today"):
        # heures communes (utilisées pour TRAVAIL, et pour ABSENCE si pas journée entière)
        colA, colB = st.columns(2)
        with colA:
            deb = st.time_input("Début", value=time(8, 30), key="add_deb")
        with colB:
            fin = st.time_input("Fin", value=time(17, 0), key="add_fin")

        absence_type = None
        # On lit l'état des toggles posés hors-form
        abs_multi = st.session_state.get("abs_multi_toggle", False)
        abs_full = st.session_state.get("abs_full_toggle", True)

        # -------- ABSENCE --------
        start_date = end_date = None
        abs_date = date.today()
        if typ == "absence":
            absence_type = st.selectbox(
                "Catégorie d'absence",
                ["vacances", "congés statutaires", "maladie", "armée-PC"],
                index=0, key="add_abs"
            )

            if abs_multi:
                cS, cE = st.columns(2)
                with cS:
                    start_date = st.date_input("Début", value=date.today(), key="abs_start")
                with cE:
                    end_date = st.date_input("Fin", value=date.today(), key="abs_end")
                st.caption("Les week-ends sont ignorés automatiquement.")
                if abs_full:
                    st.caption(f"Durée appliquée : {DAILY_WORK_HOURS_DEFAULT:.1f} h par jour ouvré.")
            else:
                abs_date = st.date_input("Date d'absence", value=date.today(), key="abs_single")
                st.caption("Les week-ends sont ignorés.")
                if abs_full:
                    st.caption(f"Durée appliquée : {DAILY_WORK_HOURS_DEFAULT:.1f} h.")

        # -------- TRAVAIL --------
        commune_label = domaine_label = dossier = note = None
        if typ == "travail":
            communes2 = session.query(Commune).order_by(Commune.nom).all()
            domaines2 = session.query(Domaine).order_by(Domaine.libelle).all()
            c1, c2 = st.columns(2)
            with c1:
                commune_label = st.selectbox("Commune", [c.nom for c in communes2], key="add_commune")
            with c2:
                domaine_label = st.selectbox("Domaine", [d.libelle for d in domaines2], key="add_domaine")
            dossier = st.text_input("Dossier / N° BTI", "", key="add_dossier")
            note = st.text_area("Note", "", height=70, key="add_note")

        # ✅ bouton submit
        ok = st.form_submit_button("Ajouter")

        if ok:
            from sqlalchemy.exc import IntegrityError

            if typ == "travail":
                duree = minutes_between(deb, fin)
                if not duree or duree <= 0:
                    st.error("Durée invalide (travail).")
                    st.stop()
                entry = TimeEntry(
                    employee_id=employee.id,
                    date=date.today(),
                    heure_debut=deb,
                    heure_fin=fin,
                    duree_min=duree,
                    type="travail",
                    absence_type=None,
                    commune_id=(session.query(Commune).filter_by(nom=commune_label).first().id if commune_label else None),
                    domaine_id=(session.query(Domaine).filter_by(libelle=domaine_label).first().id if domaine_label else None),
                    dossier=(dossier or None),
                    description=(note or None),
                )
                session.add(entry)
                try:
                    session.commit()
                    st.success("Travail ajouté ✅")
                    st.rerun()
                except IntegrityError:
                    session.rollback()
                    st.warning("Entrée de travail déjà existante (doublon).")
                    st.stop()

            # ----- ABSENCE -----
            else:
                results = {"created": 0, "skipped": 0, "errors": 0}

                def add_abs(d: date, minutes: int):
                    e = TimeEntry(
                        employee_id=employee.id,
                        date=d,
                        heure_debut=None if minutes else deb,
                        heure_fin=None if minutes else fin,
                        duree_min=minutes if minutes else minutes_between(deb, fin),
                        type="absence",
                        absence_type=absence_type,
                        commune_id=None,
                        domaine_id=None,
                        dossier=None,
                        description=None,
                    )
                    if e.duree_min is None or e.duree_min <= 0:
                        results["errors"] += 1
                        return
                    session.add(e)
                    try:
                        session.commit()
                        results["created"] += 1
                    except IntegrityError:
                        session.rollback()
                        results["skipped"] += 1

                if abs_multi:
                    if not start_date or not end_date:
                        st.error("Sélectionnez Début et Fin.")
                        st.stop()
                    if start_date > end_date:
                        st.error("La date de début est après la date de fin.")
                        st.stop()

                    cur = start_date
                    while cur <= end_date:
                        if cur.weekday() < 5:  # lun–ven
                            if abs_full:
                                add_abs(cur, int(DAILY_WORK_HOURS_DEFAULT * 60))
                            else:
                                add_abs(cur, 0)  # 0 => utilise deb/fin
                        cur += timedelta(days=1)

                    st.success(
                        f"Absences ajoutées : {results['created']} · doublons ignorés : {results['skipped']}"
                        + (f" · erreurs : {results['errors']}" if results['errors'] else "")
                    )
                    st.rerun()

                else:
                    if abs_date.weekday() < 5:
                        if abs_full:
                            add_abs(abs_date, int(DAILY_WORK_HOURS_DEFAULT * 60))
                        else:
                            add_abs(abs_date, 0)
                        st.success(
                            f"Absence ajoutée : {results['created']} · doublon ignoré : {results['skipped']}"
                            + (f" · erreurs : {results['errors']}" if results['errors'] else "")
                        )
                        st.rerun()
                    else:
                        st.warning("Jour sélectionné = week-end (ignoré). Choisissez un jour ouvré.")

    # --- Liste du jour (compacte)
    st.markdown("### Mes tâches du jour")
    rows = []
    for e in entries:
        rows.append({
            "Type": e.type if e.type=="travail" else f"absence – {e.absence_type or ''}",
            "Début": e.heure_debut.strftime('%H:%M') if e.heure_debut else "",
            "Fin": e.heure_fin.strftime('%H:%M') if e.heure_fin else "",
            "Durée": fmt_hhmm_from_minutes(e.duree_min),
            "Commune": e.commune.nom if e.commune else "",
            "Domaine": e.domaine.libelle if e.domaine else "",
            "Dossier": e.dossier or "",
            "Note": (e.description or "")
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


    #Débours

def page_debours(session, employee: Employee):
    st.subheader("Débours du mois")
    year = st.number_input("Année", min_value=2000, max_value=2100, value=date.today().year, key="dbs_year")
    month = st.number_input("Mois", min_value=1, max_value=12, value=date.today().month, key="dbs_month")

    st.markdown("### Ajouter un débours")
    communes = session.query(Commune).order_by(Commune.nom).all()
    with st.form("add_expense"):
        c1, c2, c3, c4 = st.columns([1,1,1,2])
        with c1: dte = st.date_input("Date", value=date.today(), key="exp_date")
        with c2: fournisseur = st.text_input("Fournisseur", key="exp_fourn")
        with c3: commune_name = st.selectbox("Commune", [c.nom for c in communes], key="exp_commune")
        with c4: montant = st.number_input("Montant (CHF)", min_value=0.0, step=0.1, key="exp_amt")
        note = st.text_input("Note (facultatif)", key="exp_note")
        ok = st.form_submit_button("Ajouter")
        if ok:
            c = session.query(Commune).filter_by(nom=commune_name).first()
            session.add(Expense(employee_id=employee.id, date=dte, fournisseur=fournisseur or None,
                                commune_id=c.id if c else None, amount=float(montant), note=note or None))
            session.commit()
            st.success("Débours ajouté ✅")
            st.rerun()

    st.markdown("### Tableau mensuel (style capture)")
    df_detail, df_wide, total = debours_mensuels(session, employee, year, month)
    if not df_wide.empty:
        st.dataframe(df_wide, use_container_width=True, hide_index=True)
        st.markdown(f"**Total débours : {total:,.2f} CHF**".replace(",", " ").replace(".", ","))  # format fr
    else:
        st.info("Aucun débours ce mois.")

    st.markdown("#### Ajouter des kilomètres (créera une ligne par commune)")
    with st.form("add_km"):
        col = st.columns(len(communes)+2)
        km_vals = {}
        for i, c in enumerate(communes):
            with col[i]:
                km_vals[c.nom] = st.number_input(f"{c.nom}", min_value=0.0, step=1.0, key=f"km_{c.id}")
        with col[-2]:
            rate = st.number_input("Tarif (CHF/km)", min_value=0.0, value=0.70, step=0.05, key="km_rate")
        with col[-1]:
            ok2 = st.form_submit_button("Ajouter lignes KM")
        if ok2:
            for cname, km in km_vals.items():
                if km and km > 0:
                    c = next(cc for cc in communes if cc.nom == cname)
                    session.add(Expense(employee_id=employee.id, date=date(year, month, 1),
                                        fournisseur=f"KM @ {rate:.2f} CHF/km", commune_id=c.id,
                                        amount=round(km*rate, 2), note=f"{km:.0f} km"))
            session.commit()
            st.success("Lignes KM ajoutées ✅")
            st.rerun()

def page_mon_mois(session, employee: Employee):
    st.subheader("Mon mois")
    year = st.number_input("Année", min_value=2000, max_value=2100, value=date.today().year, key="mm_year")
    month = st.number_input("Mois", min_value=1, max_value=12, value=date.today().month, key="mm_month")

    entries = session.query(TimeEntry).filter(
        TimeEntry.employee_id == employee.id,
        TimeEntry.date >= date(year, month, 1),
        TimeEntry.date <= date(year, month, monthrange(year, month)[1])
    ).order_by(TimeEntry.date.asc()).all()

    rows = []
    for e in entries:
        rows.append({
            "ID": e.id,
            "Date": e.date.strftime('%d.%m.%Y'),
            "Type": e.type,
            "Début": e.heure_debut.strftime('%H:%M') if e.heure_debut else "",
            "Fin": e.heure_fin.strftime('%H:%M') if e.heure_fin else "",
            "Durée": fmt_hhmm_from_minutes(e.duree_min),
            "Minutes": e.duree_min or 0,
            "Commune": e.commune.nom if e.commune else "",
            "Domaine": e.domaine.libelle if e.domaine else "",
            "Dossier": e.dossier or "",
            "Note": e.description or "",
            "Absence": e.absence_type or "",
            "Facturable": "Oui" if e.facturable is True else ("Non" if e.facturable is False else "—")
        })
    df = pd.DataFrame(rows)

    st.markdown("### Répartition du mois")
    g1, g2 = st.columns(2)
    with g1:
        by_comm = df.groupby('Commune', dropna=False)['Minutes'].sum().reset_index() if not df.empty else pd.DataFrame(columns=['Commune','Minutes'])
        st.dataframe(by_comm, use_container_width=True, hide_index=True)
    with g2:
        by_dom = df.groupby('Domaine', dropna=False)['Minutes'].sum().reset_index() if not df.empty else pd.DataFrame(columns=['Domaine','Minutes'])
        st.dataframe(by_dom, use_container_width=True, hide_index=True)

    st.markdown("### Détails du mois")
    st.dataframe(df.drop(columns=['Minutes']) if 'Minutes' in df.columns else df, use_container_width=True, hide_index=True)

    col1, col2 = st.columns([1,1])
    with col1:
        st.markdown("**Supprimer des lignes**")
        to_delete = st.multiselect("Sélection", df["ID"].tolist() if not df.empty else [], key="mm_del")
        if st.button("Supprimer", key="mm_del_btn") and to_delete:
            session.query(TimeEntry).filter(TimeEntry.id.in_(to_delete)).delete(synchronize_session=False)
            session.commit()
            st.success("Supprimé.")
            st.rerun()

    with col2:
        st.markdown("**Heures théoriques (doit)**")
        default_h = default_theoretical_hours(year, month, session=session)
        th = session.query(TheoreticalHours).filter_by(employee_id=employee.id, year=year, month=month).first()
        current = th.hours if th else default_h

        # admin only edit
        is_admin = (st.session_state.get("user_role") == "admin") or (getattr(employee, "role", "") == "admin")
        if is_admin:
            unique_key = f"mm_doit_{employee.id}_{year}_{month}"
            new_val = st.number_input("Heures (modifiable)", min_value=0.0, max_value=400.0,
                                      value=float(current), step=0.5, key=unique_key)
            if st.button("Enregistrer le doit", key=f"{unique_key}_btn"):
                if th:
                    th.hours = new_val
                else:
                    session.add(TheoreticalHours(employee_id=employee.id, year=year, month=month, hours=new_val))
                session.commit()
                st.success("Enregistré.")
        else:
            st.metric("Heures (doit)", f"{current:.1f}h")
            st.caption("Modifiable par un administrateur uniquement.")

def page_resume_export(session, employee: Employee):
    st.subheader("Résumé & Export")
    year = st.number_input("Année", min_value=2000, max_value=2100, value=date.today().year, key="rx_year")
    month = st.number_input("Mois", min_value=1, max_value=12, value=date.today().month, key="rx_month")

    # Rapports heures
    df_entries, df_comm, df_dom, df_cross, worked_minutes, absences, theoretical, extras = compute_reports(
        session, employee, year, month
    )

    # Débours
    df_debours_detail, df_debours_wide, debours_total = debours_mensuels(session, employee, year, month)

    # KPIs
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Heures effectuées (mois)", fmt_hhmm_from_minutes(worked_minutes))
    with c2:
        st.metric("Heures théoriques (mois)", f"{theoretical:.1f}h")
    with c3:
        diff = (worked_minutes/60.0) - theoretical
        st.metric("Δ mois", f"{diff:+.1f}h")
    with c4:
        st.metric("Vacances (mois)", fmt_hhmm_from_minutes(absences.get("vacances", 0)))

    st.markdown("### Cumuls année")
    cA, cB, cC = st.columns(3)
    with cA:
        st.metric("Heures effectuées (YTD)", f"{extras['ytd_minutes']/60.0:.1f}h")
    with cB:
        st.metric("Heures théoriques (YTD)", f"{extras['ytd_theoretical_hours']:.1f}h")
    with cC:
        ydiff = (extras['ytd_minutes']/60.0) - extras['ytd_theoretical_hours']
        st.metric("Δ année", f"{ydiff:+.1f}h")
    st.caption("Les heures théoriques tiennent compte des jours fériés saisis (VD + fédéraux).")

    # Détails heures
    st.markdown("### Détails (mois)")
    st.dataframe(
        df_entries.drop(columns=["Minutes"]) if "Minutes" in df_entries.columns else df_entries,
        use_container_width=True, hide_index=True
    )

    # Débours
    st.markdown("### Débours du mois")
    if not df_debours_wide.empty:
        st.dataframe(df_debours_wide, use_container_width=True, hide_index=True)
        st.markdown(f"**Total débours : {debours_total:,.2f} CHF**".replace(",", " ").replace(".", ","))
    else:
        st.info("Aucun débours ce mois.")

    # Export
    colA, colB = st.columns(2)
    with colA:
        if st.button("Générer le PDF mensuel", key="btn_pdf"):
            pdf_bytes = build_pdf(
                employee.display_name, year, month,
                df_entries.drop(columns=["Minutes"], errors="ignore"),
                df_comm, df_dom, df_cross,
                worked_minutes, absences, theoretical,
                extras=extras,
                df_debours_wide=df_debours_wide,
                debours_total=debours_total,
            )
            file_name = f"rapport_mensuel_{employee.initials or employee.display_name}_{year}-{month:02d}.pdf"
            st.download_button(
                "Télécharger le PDF", data=pdf_bytes, file_name=file_name, mime="application/pdf", key="dl_pdf"
            )

    with colB:
        if st.button("Exporter Excel (xlsx)", key="btn_xlsx"):
            out = io.BytesIO()
            with pd.ExcelWriter(out, engine="xlsxwriter") as writer:
                (df_entries.drop(columns=["Minutes"], errors="ignore")).to_excel(writer, sheet_name="Détails", index=False)
                df_comm.to_excel(writer, sheet_name="Par commune", index=False)
                df_dom.to_excel(writer, sheet_name="Par domaine", index=False)
                df_cross.to_excel(writer, sheet_name="Commune x Domaine", index=False)
                # onglet Débours
                if not df_debours_wide.empty:
                    df_debours_wide.to_excel(writer, sheet_name="Débours", index=False)
            st.download_button(
                "Télécharger Excel",
                data=out.getvalue(),
                file_name=f"export_{year}-{month:02d}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_xlsx",
            )

    # Camemberts
    st.markdown("### Par commune (répartition %)")
    if not df_comm.empty:
        total_m = df_comm["Minutes"].sum()
        df_comm["%"] = (df_comm["Minutes"] / total_m * 100).round(1)
        fig, ax = plt.subplots(figsize=(2, 2))  # ← (2,2) est plus lisible que (1,1)
        ax.pie(
            df_comm["Minutes"],
            labels=df_comm["Commune"],
            autopct=lambda p: f"{p:.1f}%",
            textprops={"fontsize": 6}
        )
        ax.set_title("Répartition par commune", fontsize=7)
        st.pyplot(fig)
    else:
        st.info("Aucune minute de travail ce mois par commune.")

    st.markdown("### Par domaine (répartition %)")
    if not df_dom.empty:
        total_m = df_dom["Minutes"].sum()
        df_dom["%"] = (df_dom["Minutes"] / total_m * 100).round(1)
        fig, ax = plt.subplots(figsize=(2, 2))  # ← bien fermé
        ax.pie(
            df_dom["Minutes"],   # ← correction ici
            labels=df_dom["Domaine"],  # ← correction ici
            autopct=lambda p: f"{p:.1f}%",
            textprops={"fontsize": 6}
        )
        ax.set_title("Répartition par domaine", fontsize=7)
        st.pyplot(fig)
    else:
        st.info("Aucune minute de travail ce mois par domaine.")

def page_dashboard(session, employee: Employee):
    st.subheader("Tableau de bord")
    year = st.number_input("Année", min_value=2000, max_value=2100, value=date.today().year, key="db_year")
    month = st.number_input("Mois", min_value=1, max_value=12, value=date.today().month, key="db_month")
    df_entries, df_comm, df_dom, df_cross, worked_minutes, absences, theoretical, extras = compute_reports(session, employee, year, month)

    c1, c2, c3, c4 = st.columns(4)
    with c1: st.metric("Heures", fmt_hhmm_from_minutes(worked_minutes))
    with c2: st.metric("Doit", f"{theoretical:.1f}h")
    with c3: st.metric("Δ mois", f"{(worked_minutes/60.0 - theoretical):+.1f}h")
    with c4: st.metric("Absences", fmt_hhmm_from_minutes(sum(absences.values())))

    c5, c6 = st.columns(2)
    with c5: st.metric("Heures (YTD)", f"{extras['ytd_minutes']/60.0:.1f}h")
    with c6: st.metric("Δ année", f"{(extras['ytd_minutes']/60.0 - extras['ytd_theoretical_hours']):+.1f}h")

    if not df_comm.empty:
        fig, ax = plt.subplots(figsize=(6,3))
        ax.bar(df_comm['Commune'], df_comm['Minutes']); ax.set_ylabel('Minutes'); ax.set_title('Commune'); fig.tight_layout()
        st.pyplot(fig)

    if not df_dom.empty:
        fig, ax = plt.subplots(figsize=(6,3))
        ax.bar(df_dom['Domaine'], df_dom['Minutes']); ax.set_ylabel('Minutes'); ax.set_title('Domaine'); fig.tight_layout()
        st.pyplot(fig)

def page_admin(session, me: Employee):
    st.subheader("Administration")
    employees = session.query(Employee).order_by(Employee.display_name).all()
    
    st.markdown("### Collaborateurs & mots de passe")
    with st.form("add_emp"):
        name = st.text_input("Nom affiché", key="adm_add_name")
        initials = st.text_input("Initiales", max_chars=6, key="adm_add_initials")
        email = st.text_input("Email @b-t-i.ch", placeholder="prenom.nom@b-t-i.ch", key="adm_add_email")
        role = st.selectbox("Rôle", ["user","admin"], index=0, key="adm_add_role")
        pwd = st.text_input("Mot de passe (optionnel)", type="password", key="adm_add_pwd")
        pending = st.checkbox("Compte en attente de validation", value=False, key="adm_add_pending")
        if st.form_submit_button("Ajouter collaborateur", use_container_width=True):
                if not name:
                    st.error("Nom obligatoire.")
                elif email and not email.lower().endswith("@b-t-i.ch"):
                    st.error("Email doit se terminer par @b-t-i.ch")
                else:
                    hpw = bcrypt.hashpw(pwd.encode('utf-8'), bcrypt.gensalt()).decode('utf-8') if (pwd and bcrypt) else None
                    session.add(Employee(display_name=name, initials=initials or None, email=email or None, role=role, hashed_password=hpw, pending=pending, active=True))
                    session.commit()
                    st.success("Ajouté.")

    st.markdown("### Modifier un collaborateur (hors mot de passe)")
    employees = session.query(Employee).order_by(Employee.display_name).all()
    if employees:
        # Sélection de l'utilisateur à modifier
        who_edit = st.selectbox("Sélection", [e.display_name for e in employees], key="adm_edit_who")
        uedit = next(e for e in employees if e.display_name == who_edit)

        # Valeurs pré-remplies
        colA, colB, colC = st.columns(3)
        with colA:
            new_name = st.text_input("Nom affiché", value=uedit.display_name, key="adm_edit_name")
            new_initials = st.text_input("Initiales", value=uedit.initials or "", key="adm_edit_initials")
        with colB:
            new_email = st.text_input("Email (idéalement @b-t-i.ch)", value=uedit.email or "", key="adm_edit_email")
            new_role = st.selectbox("Rôle", ["user", "admin"], index=(0 if uedit.role != "admin" else 1), key="adm_edit_role")
        with colC:
            new_active = st.checkbox("Actif", value=bool(uedit.active), key="adm_edit_active")
            new_pending = st.checkbox("En attente (à valider)", value=bool(uedit.pending), key="adm_edit_pending")

        if st.button("Enregistrer les modifications", key="adm_edit_save"):
            # Protections simples
            # 1) Empêcher de se retirer soi-même le rôle admin s'il n'y a qu'un seul admin
            if uedit.role == "admin" and new_role != "admin":
                # Combien d'admins actuellement actifs ?
                admin_count = session.query(Employee).filter(Employee.role == "admin", Employee.active == True).count()
                if admin_count <= 1 and me.id == uedit.id:
                    st.error("Impossible de retirer le dernier admin actif (votre propre compte).")
                    st.stop()

            # 2) Optionnel : imposer le domaine email b-t-i.ch si renseigné
            if new_email and ("@" not in new_email or "." not in new_email):
                st.error("Email invalide.")
                st.stop()

            # 3) Unicité du nom affiché
            name_taken = session.query(Employee).filter(Employee.display_name == new_name, Employee.id != uedit.id).first()
            if name_taken:
                st.error("Ce nom affiché est déjà utilisé.")
                st.stop()

            # Appliquer les modifications
            uedit.display_name = new_name.strip()
            uedit.initials = (new_initials.strip() or None)
            uedit.email = (new_email.strip() or None)
            uedit.role = new_role
            uedit.active = bool(new_active)
            uedit.pending = bool(new_pending)

            try:
                session.commit()
                st.success("Collaborateur mis à jour ✅")
                st.rerun()
            except Exception as e:
                session.rollback()
                st.error(f"Erreur lors de la mise à jour : {e}")

    st.markdown("### Jours fériés (VD + fédéral)")
    with st.form("holi_form"):
        hd = st.date_input("Date du férié", key="holi_date")
        hl = st.text_input("Libellé", key="holi_label")
        hc = st.text_input("Canton (ex: VD ou vide pour fédéral)", value="VD", key="holi_canton")
        if st.form_submit_button("Ajouter férié"):
            if not hd or not hl:
                st.error("Date et libellé requis.")
            else:
                session.add(Holiday(date=hd, label=hl, canton=(hc or None)))
                try:
                    session.commit()
                    st.success("Férié ajouté.")
                except Exception as e:
                    session.rollback()
                    st.error(f"Erreur: {e}")
    hol = session.query(Holiday).order_by(Holiday.date.asc()).all()
    if hol:
        dfh = pd.DataFrame([{ 'Date': h.date.strftime('%d.%m.%Y'), 'Libellé': h.label, 'Canton': h.canton or '—'} for h in hol])
        st.dataframe(dfh, use_container_width=True, hide_index=True)

    st.markdown("### Communes & Domaines")
    c1, c2 = st.columns(2)
    with c1:
        with st.form("add_com"):
            nom = st.text_input("Nom de la commune", key="adm_com_nom")
            if st.form_submit_button("Ajouter commune"):
                if not nom:
                    st.error("Nom requis.")
                else:
                    session.add(Commune(nom=nom)); session.commit(); st.success("Ajouté.")
    with c2:
        with st.form("add_dom"):
            lib = st.text_input("Libellé du domaine", key="adm_dom_lib")
            if st.form_submit_button("Ajouter domaine"):
                if not lib:
                    st.error("Libellé requis.")
                else:
                    session.add(Domaine(libelle=lib)); session.commit(); st.success("Ajouté.")

    st.markdown("### Budgets (annuels, en heures)")
    communes = session.query(Commune).order_by(Commune.nom).all()
    domaines = session.query(Domaine).order_by(Domaine.libelle).all()
    with st.form("add_budget"):
        colA, colB, colC, colD = st.columns(4)
        with colA:
            bc = st.selectbox("Commune", [c.nom for c in communes], key="adm_bud_commune")
        with colB:
            bd = st.selectbox("Domaine", [d.libelle for d in domaines], key="adm_bud_domaine")
        with colC:
            by = st.number_input("Année", min_value=2000, max_value=2100, value=date.today().year, key="adm_bud_year")
        with colD:
            bh = st.number_input("Budget (h)", min_value=0.0, max_value=10000.0, value=0.0, step=0.5, key="adm_bud_hours")
        if st.form_submit_button("Enregistrer budget"):
            c = session.query(Commune).filter_by(nom=bc).first()
            d = session.query(Domaine).filter_by(libelle=bd).first()
            b = session.query(Budget).filter_by(commune_id=c.id, domaine_id=d.id, year=int(by)).first()
            if b:
                b.hours = float(bh)
            else:
                session.add(Budget(commune_id=c.id, domaine_id=d.id, year=int(by), hours=float(bh)))
            session.commit(); st.success("Budget enregistré.")

    st.markdown("### Verrouillage mensuel (approbation)")
    with st.form("lock_form"):
        e = st.selectbox("Collaborateur", [x.display_name for x in employees], key="adm_lock_user")
        y = st.number_input("Année", min_value=2000, max_value=2100, value=date.today().year, key="adm_lock_year")
        m = st.number_input("Mois", min_value=1, max_value=12, value=date.today().month, key="adm_lock_month")
        lock = st.checkbox("Verrouiller ce mois", value=True, key="adm_lock_flag")
        if st.form_submit_button("Appliquer verrouillage"):
            emp = next(x for x in employees if x.display_name == e)
            ml = session.query(MonthLock).filter_by(employee_id=emp.id, year=int(y), month=int(m)).first()
            if ml:
                ml.locked = lock
            else:
                session.add(MonthLock(employee_id=emp.id, year=int(y), month=int(m), locked=lock))
            session.commit(); st.success("État appliqué.")

# -----------------------------
# Main
# -----------------------------
def main():
    st.set_page_config(page_title=APP_TITLE, layout="wide")

    # petit thème CSS ...
    st.markdown(""" ... """, unsafe_allow_html=True)

    st.title(APP_TITLE)
    st.caption("Edition pro – saisie, timer, planning, rapports et export.")

    session = Session()

    # Auth
    user = current_user(session)
    if not user:
        login_form()
        session.close()
        return
    st.session_state["user_role"] = user.role

    st.sidebar.write(f"Connecté: **{user.display_name}** ({user.role})")
    if user.pending:
        st.sidebar.warning("⚠️ Votre compte est encore en attente (certaines actions peuvent être restreintes).")
    if st.sidebar.button("Se déconnecter", key="logout"):
        st.session_state.pop("user_id", None)
        st.rerun()

    # Navigation
    options = [
        "Planning du jour",
        "Débours",
        "Mon mois",
        "Résumé & Export",
        "Tableau de bord",
    ]
    if user.role == "admin":
        options.append("Administration")

    page = st.sidebar.radio("Navigation", options, index=0, key="nav_radio")

    # ✅ Ces conditions doivent être au même niveau d’indentation que `page = ...`
    if page == "Planning du jour":
        page_planning_jour(session, user)
    elif page == "Débours":
        page_debours(session, user)
    elif page == "Mon mois":
        page_mon_mois(session, user)
    elif page == "Résumé & Export":
        page_resume_export(session, user)
    elif page == "Tableau de bord":
        page_dashboard(session, user)
    elif page == "Administration" and user.role == "admin":
        page_admin(session, user)
    else:
        st.info("Section non disponible.")

    session.close()

if __name__ == "__main__":
    main()
