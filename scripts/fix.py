import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

# Borrar el registro de pierna corta — el spread es UN solo registro
cur.execute("DELETE FROM positions WHERE id = 5")

# Dejar id 6 como el spread completo, con el tastytrade_symbol de la pierna larga
cur.execute("""
    UPDATE positions SET
        strategy          = 'Bull Call Spread',
        strike_low        = 75.0,
        strike_high       = 79.0,
        contracts         = 1,
        premium_paid      = 1.54,
        total_cost        = 154.0,
        price_at_open     = 74.41,
        notes             = 'Bull Call Spread XYZ $75/$79 Jul2. Debito neto $1.54.'
    WHERE id = 6
""")

conn.commit()
print('OK — spread consolidado en un solo registro (id 6)')
cur.close()
conn.close()