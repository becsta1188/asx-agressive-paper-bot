"""ASX paper-trading experiment only. No broker connection or real orders."""
import datetime as dt
import math
import sqlite3
from zoneinfo import ZoneInfo
import yfinance as yf

DB = 'aggressive_trades.sqlite3'
START = 5000.0
FEE = 5.0
SYDNEY = ZoneInfo('Australia/Sydney')
GROUPS = {
    'Aggressive': ['DRO.AX', 'PLS.AX', 'LTR.AX', 'ZIP.AX'],
}
SETTINGS = {
    'Aggressive': dict(fraction=.40, max_positions=3, momentum=.05, volume_ratio=1.5, stop=.08, target=.15),
}

def init(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS portfolios (name TEXT PRIMARY KEY, cash REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS positions (portfolio TEXT, ticker TEXT, shares INTEGER, entry REAL, PRIMARY KEY(portfolio,ticker));
    CREATE TABLE IF NOT EXISTS pending (portfolio TEXT, ticker TEXT, action TEXT, signal_date TEXT, PRIMARY KEY(portfolio,ticker));
    CREATE TABLE IF NOT EXISTS trades (date TEXT, portfolio TEXT, ticker TEXT, action TEXT, shares INTEGER, price REAL, fee REAL, reason TEXT);
    CREATE TABLE IF NOT EXISTS snapshots (date TEXT, portfolio TEXT, cash REAL, holdings REAL, total REAL, PRIMARY KEY(date,portfolio));
    ''')
    for name in GROUPS:
        db.execute('INSERT OR IGNORE INTO portfolios VALUES (?,?)', (name, START))
    db.commit()

def get_history(ticker, today):
    h = yf.Ticker(ticker).history(period='4mo', interval='1d', auto_adjust=True)
    h = h.dropna(subset=['Open', 'Close', 'Volume']).copy()
    if len(h) < 26:
        raise ValueError('Insufficient history')
    if h.index.tz is not None:
        h.index = h.index.tz_convert(SYDNEY)
    day = h.index[-1].date()
    if day > today or (today - day).days > 4:
        raise ValueError(f'Stale/future price: {day}')
    return h

def main():
    now = dt.datetime.now(SYDNEY)
    if now.weekday() >= 5 or now.time() < dt.time(16, 30):
        print('Outside post-close weekday window; no trades')
        return
    today = now.date()
    with sqlite3.connect(DB) as db:
        init(db)
        prices = {}
        for ticker in dict.fromkeys(t for tickers in GROUPS.values() for t in tickers):
            try:
                h = get_history(ticker, today)
                day = h.index[-1].date().isoformat()
                prices[ticker] = (h, day, float(h['Close'].iloc[-1]))
                print(f'PRICE {ticker} {day}: A${prices[ticker][2]:.2f}')
            except Exception as exc:
                print(f'SKIP {ticker}: {exc}')
        if not prices:
            raise RuntimeError('No usable prices')

        for group, tickers in GROUPS.items():
            cfg = SETTINGS[group]
            cash = float(db.execute('SELECT cash FROM portfolios WHERE name=?', (group,)).fetchone()[0])
            positions = {t: (int(q), float(e)) for t, q, e in db.execute(
                'SELECT ticker,shares,entry FROM positions WHERE portfolio=?', (group,))}
            pending = {t: (a, d) for t, a, d in db.execute(
                'SELECT ticker,action,signal_date FROM pending WHERE portfolio=?', (group,))}
            latest = {prices[t][1] for t in tickers if t in prices}
            if len(latest) != 1 or len([t for t in tickers if t in prices]) != len(tickers):
                print(f'{group}: inconsistent or missing market sessions; no trading')
                continue
            day = next(iter(latest))
            # Never reprocess the same market session, even on a manually rerun workflow.
            processed = db.execute('SELECT 1 FROM snapshots WHERE portfolio=? AND date=?', (group, day)).fetchone()
            if processed:
                print(f'{group}: already processed {day}')
                continue

            # Previously generated signals execute on a subsequent observed close.
            # This is an illustrative fill, not an executable live/backtest price.
            for ticker in tickers:
                if ticker not in prices or ticker not in pending:
                    continue
                action, signal_day = pending[ticker]
                if signal_day >= day:
                    continue
                fill = prices[ticker][2]
                if action == 'SELL' and ticker in positions:
                    qty, _ = positions.pop(ticker)
                    cash += qty * fill - FEE
                    db.execute('DELETE FROM positions WHERE portfolio=? AND ticker=?', (group, ticker))
                    db.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)',
                               (day, group, ticker, 'SELL', qty, fill, FEE, 'prior-session exit'))
                    print(f'{group}: PAPER SELL {qty} {ticker} at A${fill:.2f}')
                elif action == 'BUY' and ticker not in positions and len(positions) < cfg['max_positions']:
                    budget = min(START * cfg['fraction'], cash)
                    qty = max(0, math.floor((budget - FEE) / fill))
                    if qty > 0 and qty * fill + FEE <= cash:
                        cash -= qty * fill + FEE
                        positions[ticker] = (qty, fill)
                        db.execute('INSERT INTO positions VALUES (?,?,?,?)', (group, ticker, qty, fill))
                        db.execute('INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)',
                                   (day, group, ticker, 'BUY', qty, fill, FEE, 'prior-session momentum'))
                        print(f'{group}: PAPER BUY {qty} {ticker} at A${fill:.2f}')
                    else:
                        print(f'{group}: NO BUY {ticker}: insufficient allocation')
                db.execute('DELETE FROM pending WHERE portfolio=? AND ticker=?', (group, ticker))

            candidates = []
            for ticker in tickers:
                if ticker not in prices or ticker in pending and pending[ticker][1] == day:
                    continue
                h, _, close = prices[ticker]
                avg20 = float(h['Close'].tail(20).mean())
                ret5 = close / float(h['Close'].iloc[-6]) - 1
                if ticker in positions:
                    _, entry = positions[ticker]
                    if close <= entry * (1 - cfg['stop']) or close >= entry * (1 + cfg['target']) or close < avg20:
                        db.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?,?)',
                                   (group, ticker, 'SELL', day))
                        print(f'{group}: SELL signal {ticker} on {day}')
                elif close > avg20 and ret5 > cfg['momentum']:
                    prior_volume = float(h['Volume'].iloc[-21:-1].mean())
                    volume_ratio = float(h['Volume'].iloc[-1]) / prior_volume if prior_volume > 0 else 0
                    if volume_ratio >= cfg['volume_ratio']:
                        candidates.append((ret5, ticker))
            # Rank new buy candidates by strongest five-session momentum.
            pending_buys = db.execute("SELECT COUNT(*) FROM pending WHERE portfolio=? AND action='BUY'", (group,)).fetchone()[0]
            available = max(0, cfg['max_positions'] - len(positions) - pending_buys)
            for _, ticker in sorted(candidates, reverse=True)[:available]:
                db.execute('INSERT OR REPLACE INTO pending VALUES (?,?,?,?)',
                           (group, ticker, 'BUY', day))
                print(f'{group}: BUY signal {ticker} on {day}')

            db.execute('UPDATE portfolios SET cash=? WHERE name=?', (cash, group))
            missing = [ticker for ticker in positions if ticker not in prices]
            if missing:
                print(f'{group}: cannot value holdings; missing quotes: {missing}')
            else:
                holdings = sum(q * prices[t][2] for t, (q, _) in positions.items())
                total = cash + holdings
                db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?)', (day, group, cash, holdings, total))
                print(f'{group}: cash A${cash:.2f}, holdings A${holdings:.2f}, total A${total:.2f}')
        db.commit()

if __name__ == '__main__':
    main()
