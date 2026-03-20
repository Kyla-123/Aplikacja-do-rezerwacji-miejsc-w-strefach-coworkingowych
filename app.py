"""Backend aplikacji do rezerwacji stanowisk pracy.

Plik zawiera konfigurację Flask, obsługę sesji użytkownika, dostęp do bazy SQLite
oraz endpointy API wykorzystywane przez interfejs webowy.
"""

from flask import Flask, request, jsonify, session, make_response
from flask_cors import CORS
import sqlite3
import os
from datetime import datetime, date, time

# Inicjalizacja aplikacji serwerowej oraz głównego punktu wejścia dla endpointów API.
app = Flask(__name__)

# W wersji produkcyjnej klucz powinien być przekazywany przez zmienną środowiskową.
app.secret_key = os.getenv('SECRET_KEY', 'dev_secret_key_change_me')

# Podstawowe ustawienia ciasteczek sesyjnych (dla środowiska lokalnego).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)
# Konfiguracja CORS pozwala frontendowi działającemu lokalnie komunikować się z backendem
# z zachowaniem ciasteczek sesyjnych.
CORS(app, supports_credentials=True, origins=["http://127.0.0.1:8080"])

# Ścieżka do lokalnej bazy SQLite przechowującej stanowiska, rezerwacje i ustawienia użytkowników.
DB = 'stanowiska.db'


# Funkcja przygotowuje strukturę bazy danych przy uruchomieniu aplikacji
# i uzupełnia listę stanowisk startowych.
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # Główna tabela stanowisk
    c.execute("""CREATE TABLE IF NOT EXISTS stanowiska (
        id INTEGER PRIMARY KEY,
        occupied INTEGER DEFAULT 0,
        reserved INTEGER DEFAULT 0,
        reserved_by TEXT,
        reserved_from TEXT,
        reserved_to TEXT
    )""")
    # biurka 1–40 (20 na każde piętro)
    for i in range(1, 41):
        c.execute('INSERT OR IGNORE INTO stanowiska (id) VALUES (?)', (i,))

    # tabela ustawień użytkowników
    c.execute("""CREATE TABLE IF NOT EXISTS user_settings (
        username TEXT PRIMARY KEY,
        default_duration INTEGER DEFAULT 1,
        notifications INTEGER DEFAULT 1
    )""")

    # NOWA tabela rezerwacji – wiele rezerwacji na to samo biurko w różnych dniach
    c.execute("""CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        desk_id INTEGER NOT NULL,
        reserved_by TEXT NOT NULL,
        reserved_from TEXT NOT NULL,
        reserved_to TEXT NOT NULL
    )""")

    conn.commit()
    conn.close()


# Funkcja porządkowa pozostawiona jako punkt rozszerzenia.
# W obecnej wersji status stanowisk jest wyliczany dynamicznie na podstawie zakresów dat.
def clean_expired():
    """
    Status biurek jest  liczony na podstawie daty wybranej w kalendarzu.
    """
    return


# Hook wykonywany przed każdym żądaniem.
# Pozwala w jednym miejscu uruchamiać logikę porządkową wspólną dla całej aplikacji.
@app.before_request
def before_request():
    clean_expired()


# Endpoint logowania zapisuje uproszczoną sesję użytkownika i zapewnia domyślne ustawienia
# dla nowego konta, tak aby frontend mógł od razu pobrać komplet danych startowych.
@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username') if data else None
    if not username:
        return jsonify({"error": "Brak nazwy użytkownika"}), 400
    session['username'] = username

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO user_settings (username) VALUES (?)", (username,))
    conn.commit()

    c.execute("SELECT default_duration, notifications FROM user_settings WHERE username=?", (username,))
    row = c.fetchone()
    conn.close()
    default_duration, notifications = row if row else (1, 1)

    resp = make_response(jsonify({
        "message": "Zalogowano",
        "username": username,
        "default_duration": default_duration,
        "notifications": bool(notifications)
    }))
    return resp


# Endpoint wylogowania usuwa użytkownika z sesji po stronie backendu.
@app.route('/api/logout', methods=['POST'])
def logout_api():
    session.pop('username', None)
    return jsonify({"message": "Wylogowano"}), 200


# Prosty endpoint weryfikujący aktywną sesję oraz zwracający nazwę zalogowanego użytkownika.
@app.route('/api/me', methods=['GET'])
def me():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403
    return jsonify({"username": session['username']}), 200


# Endpoint zwraca stan wszystkich stanowisk dla wybranego dnia.
# Dane są agregowane z tabeli rezerwacji i służą do odświeżania głównego widoku biura.
@app.route('/api/status', methods=['GET'])
@app.route('/api/get_status', methods=['GET'])
def get_status():
    """
    Zwraca status wszystkich biurek dla wskazanej daty (lub dzisiejszej,
    jeśli parametr 'date' nie został podany).

    Każdy wiersz ma postać:
    [id_stanowiska, occupied(0), reserved(0/1), reserved_by, reserved_from, reserved_to]
    """
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403

    clean_expired()

    date_str = request.args.get('date')
    if date_str:
        try:
            selected_date = datetime.fromisoformat(date_str).date()
        except ValueError:
            return jsonify({"error": "Nieprawidłowy format daty (użyj RRRR-MM-DD)"}), 400
    else:
        selected_date = date.today()

    # Dla wskazanej daty wyznaczany jest pełny zakres dobowy, aby łatwo sprawdzić
    # czy istnieją rezerwacje nachodzące na wybrany dzień.
    # zakres doby
    day_start = datetime.combine(selected_date, time.min).isoformat(timespec='seconds')
    day_end = datetime.combine(selected_date, time.max).isoformat(timespec='seconds')

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # Zapytanie buduje wynik dla każdego stanowiska osobno.
    # Pole occupied pozostaje technicznym placeholderem, natomiast reserved i dane szczegółowe
    # są wyliczane na podstawie rekordów z tabeli reservations.
    # occupied zawsze 0, reserved wyznaczamy z tabeli reservations
    c.execute("""
        SELECT
            s.id,
            0 AS occupied,
            CASE WHEN EXISTS (
                SELECT 1 FROM reservations r
                WHERE r.desk_id = s.id
                  AND r.reserved_from <= ?
                  AND r.reserved_to   >= ?
            ) THEN 1 ELSE 0 END AS reserved,
            (
                SELECT r.reserved_by
                FROM reservations r
                WHERE r.desk_id = s.id
                  AND r.reserved_from <= ?
                  AND r.reserved_to   >= ?
                ORDER BY r.reserved_from
                LIMIT 1
            ) AS reserved_by,
            (
                SELECT r.reserved_from
                FROM reservations r
                WHERE r.desk_id = s.id
                  AND r.reserved_from <= ?
                  AND r.reserved_to   >= ?
                ORDER BY r.reserved_from
                LIMIT 1
            ) AS reserved_from,
            (
                SELECT r.reserved_to
                FROM reservations r
                WHERE r.desk_id = s.id
                  AND r.reserved_from <= ?
                  AND r.reserved_to   >= ?
                ORDER BY r.reserved_from
                LIMIT 1
            ) AS reserved_to
        FROM stanowiska s
        ORDER BY s.id
    """, (
        day_end, day_start,   # EXISTS
        day_end, day_start,   # reserved_by
        day_end, day_start,   # reserved_from
        day_end, day_start    # reserved_to
    ))
    rows = c.fetchall()
    conn.close()
    return jsonify(rows)


# Endpoint zapisuje nową rezerwację po stronie backendu.
# Zawiera podstawową walidację danych wejściowych oraz kontrolę konfliktów czasowych.
@app.route('/api/reserve', methods=['POST'])
def reserve():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403

    data = request.get_json() or {}
    desk_id = data.get('id')
    start = data.get('start')
    end = data.get('end')
    if not all([desk_id, start, end]):
        return jsonify({"error": "Brak danych"}), 400

    try:
        desk_id = int(desk_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowe ID stanowiska"}), 400

    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        return jsonify({"error": "Nieprawidłowy format daty"}), 400

    if start_dt >= end_dt:
        return jsonify({"error": "Czas startu musi być wcześniejszy niż czas zakończenia"}), 400

    # Rezerwacja tylko w obrębie jednego dnia
    if start_dt.date() != end_dt.date():
        return jsonify({"error": "Rezerwacja może dotyczyć tylko jednej daty"}), 400

    # Biuro nieczynne w soboty i niedziele
    if start_dt.weekday() >= 5:  # 5 = sobota, 6 = niedziela
        return jsonify({"error": "Nasze biuro jest nieczynne w soboty i niedziele."}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # sprawdź, czy takie stanowisko istnieje
    c.execute("SELECT 1 FROM stanowiska WHERE id=?", (desk_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({"error": "Brak takiego stanowiska"}), 404

    # Konflikt sprawdzany jest przez wyszukanie dowolnej rezerwacji, która nachodzi czasowo
    # na nowo zgłaszany przedział dla tego samego stanowiska.
    # sprawdź konflikt czasowy z istniejącymi rezerwacjami (na tym samym stanowisku)
    c.execute("""
        SELECT COUNT(*)
        FROM reservations
        WHERE desk_id = ?
          AND NOT (reserved_to <= ? OR reserved_from >= ?)
    """, (
        desk_id,
        start_dt.isoformat(timespec='seconds'),
        end_dt.isoformat(timespec='seconds')
    ))
    (cnt,) = c.fetchone()
    if cnt > 0:
        conn.close()
        return jsonify({"error": "Stanowisko jest już zarezerwowane w tym przedziale czasu"}), 409

    # zapis nowej rezerwacji
    c.execute("""
        INSERT INTO reservations (desk_id, reserved_by, reserved_from, reserved_to)
        VALUES (?, ?, ?, ?)
    """, (
        desk_id,
        session['username'],
        start_dt.isoformat(timespec='seconds'),
        end_dt.isoformat(timespec='seconds')
    ))
    conn.commit()
    conn.close()
    return jsonify({"message": "Zarezerwowano"}), 200



# Endpoint zwraca listę rezerwacji aktualnie zalogowanego użytkownika
# do wyświetlenia w sekcji 'Moje rezerwacje'.
@app.route('/api/my_reservations', methods=['GET'])
def my_reservations():
    if 'username' not in session:
        return jsonify([]), 403
    clean_expired()
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # Zwracamy listę rezerwacji użytkownika:
    # [reservation_id, desk_id, reserved_from, reserved_to]
    c.execute("""
        SELECT id, desk_id, reserved_from, reserved_to
        FROM reservations
        WHERE reserved_by = ?
        ORDER BY reserved_from
    """, (session['username'],))
    rows = c.fetchall()
    conn.close()
    return jsonify(rows)


# Endpoint umożliwia użytkownikowi anulowanie wyłącznie własnej rezerwacji.
# Dodatkowe sprawdzenie właściciela wpisu ogranicza możliwość usuwania cudzych danych.
@app.route('/api/cancel', methods=['POST'])
def cancel():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403

    data = request.get_json() or {}
    res_id = data.get('id')
    if not res_id:
        return jsonify({"error": "Brak ID rezerwacji"}), 400

    try:
        res_id = int(res_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowe ID rezerwacji"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        SELECT reserved_by FROM reservations WHERE id = ?
    """, (res_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Brak takiej rezerwacji"}), 404

    (reserved_by,) = row
    if reserved_by != session['username']:
        conn.close()
        return jsonify({"error": "Nie możesz anulować tej rezerwacji"}), 403

    c.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Anulowano rezerwację"}), 200


# Endpoint administracyjny zwraca pełną listę rezerwacji niezależnie od właściciela.
# Dane są wykorzystywane przez panel administratora i widok kalendarza.
@app.route('/api/admin/reservations', methods=['GET'])
def admin_reservations():
    if session.get('username') != 'admin':
        return jsonify([]), 403
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # Wszystkie rezerwacje:
    # [reservation_id, desk_id, reserved_by, reserved_from, reserved_to]
    c.execute("""
        SELECT id, desk_id, reserved_by, reserved_from, reserved_to
        FROM reservations
        ORDER BY reserved_from
    """
    )
    rows = c.fetchall()
    conn.close()
    return jsonify(rows)


# Endpoint administracyjny pozwala usunąć dowolną rezerwację po identyfikatorze.
@app.route('/api/admin/cancel', methods=['POST'])
def admin_cancel():
    if session.get('username') != 'admin':
        return jsonify({"error": "Brak uprawnień"}), 403

    data = request.get_json() or {}
    res_id = data.get('id')
    if not res_id:
        return jsonify({"error": "Brak ID rezerwacji"}), 400

    try:
        res_id = int(res_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Nieprawidłowe ID rezerwacji"}), 400

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT 1 FROM reservations WHERE id=?", (res_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "Brak takiej rezerwacji"}), 404

    c.execute("DELETE FROM reservations WHERE id=?", (res_id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Rezerwacja anulowana przez admina"}), 200


# Endpoint generuje prosty eksport CSV wszystkich rezerwacji.
# Plik może zostać pobrany bezpośrednio z poziomu interfejsu użytkownika.
@app.route('/api/export_csv', methods=['GET'])
def export_csv():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403

    import csv
    from io import StringIO

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # eksportujemy wszystkie rezerwacje (po jednej linii na każdą),
   
    c.execute("""
        SELECT desk_id, 0 AS occupied, 1 AS reserved,
               reserved_by, reserved_from, reserved_to
        FROM reservations
        ORDER BY desk_id, reserved_from
    """)
    rows = c.fetchall()
    conn.close()

    output = StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['id', 'occupied', 'reserved', 'reserved_by', 'reserved_from', 'reserved_to'])
    for r in rows:
        writer.writerow(r)

    csv_data = output.getvalue()
    resp = make_response(csv_data)
    resp.headers['Content-Type'] = 'text/csv; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="rezerwacje.csv"'
    return resp


# Endpoint odczytuje zapisane preferencje użytkownika wykorzystywane w widoku ustawień.
@app.route('/api/settings', methods=['GET'])
def get_settings():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT default_duration, notifications FROM user_settings WHERE username=?", (session['username'],))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({"default_duration": 1, "notifications": True})
    default_duration, notifications = row
    return jsonify({
        "default_duration": default_duration,
        "notifications": bool(notifications)
    })


# Endpoint aktualizuje ustawienia użytkownika, zachowując jeden rekord na użytkownika
# dzięki mechanizmowi UPSERT w SQLite.
@app.route('/api/settings', methods=['POST'])
def update_settings():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403

    data = request.get_json() or {}
    default_duration = int(data.get('default_duration', 1))
    notifications = 1 if data.get('notifications', True) else 0

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
      INSERT INTO user_settings (username, default_duration, notifications)
      VALUES (?, ?, ?)
      ON CONFLICT(username) DO UPDATE SET
        default_duration=excluded.default_duration,
        notifications=excluded.notifications
    """, (session['username'], default_duration, notifications))
    conn.commit()
    conn.close()
    return jsonify({"message": "Ustawienia zapisane"})


# Endpoint przygotowuje zbiorcze statystyki do osobnego widoku analitycznego.
# Zwraca liczbę rezerwacji, najpopularniejsze stanowisko oraz rozkład dni i godzin.
@app.route('/api/stats', methods=['GET'])
def stats():
    if 'username' not in session:
        return jsonify({"error": "Nie jesteś zalogowany"}), 403
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # łączna liczba rezerwacji
    c.execute("SELECT COUNT(*) FROM reservations")
    total = c.fetchone()[0]

    # najbardziej oblegane stanowisko
    c.execute("""
        SELECT desk_id, COUNT(*) as cnt
        FROM reservations
        GROUP BY desk_id
        ORDER BY cnt DESC
        LIMIT 1
    """)
    row = c.fetchone()
    top_station = None
    if row:
        top_station = {"id": row[0], "count": row[1]}

    # rozkład wg dni tygodnia i godzin
    c.execute("SELECT reserved_from FROM reservations")
    byDay = {}
    byHour = {}
    for (fr,) in c.fetchall():
        dt = datetime.fromisoformat(fr)
        weekday_map = {0: 'Pon', 1: 'Wt', 2: 'Śr', 3: 'Czw', 4: 'Pt', 5: 'Sob', 6: 'Nd'}
        dy = weekday_map.get(dt.weekday(), str(dt.weekday()))
        hr = dt.hour
        byDay[dy] = byDay.get(dy, 0) + 1
        byHour[hr] = byHour.get(hr, 0) + 1

    conn.close()
    return jsonify({"total": total, "topStation": top_station, "byDay": byDay, "byHour": byHour})


# Przy bezpośrednim uruchomieniu pliku inicjalizowana jest baza i startuje lokalny serwer developerski.
if __name__ == '__main__':
    init_db()
    app.run(debug=True)
