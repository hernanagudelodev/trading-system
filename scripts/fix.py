import os
from dotenv import load_dotenv
import psycopg2
load_dotenv()
conn = psycopg2.connect(os.getenv('DATABASE_URL'))
cur = conn.cursor()

# Pierna larga: $75 Call — costo real del spread completo
cur.execute("""
    UPDATE positions SET
        strategy     = 'Bull Call Spread',
        strike_low   = 75.0,
        strike_high  = 79.0,
        premium_paid = 1.54,
        total_cost   = 154.0,
        notes        = 'Bull Call Spread XYZ $75/$79 Jul2. Long leg. Filled ~$1.54 db.'
    WHERE id = 6
""")

# Pierna corta: $79 Call — marcar como short leg
cur.execute("""
    UPDATE positions SET
        strategy     = 'Bull Call Spread (short leg)',
        strike_low   = 75.0,
        strike_high  = 79.0,
        premium_paid = -2.23,
        total_cost   = -223.0,
        notes        = 'Bull Call Spread XYZ $75/$79 Jul2. Short leg.'
    WHERE id = 5
""")

conn.commit()
print('OK — posiciones actualizadas')
cur.close()
conn.close()