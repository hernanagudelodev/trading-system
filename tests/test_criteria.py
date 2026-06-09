from criteria import get_all_criteria
from scoring import score_criteria
import json

criteria = get_all_criteria("MSFT")
result = score_criteria(criteria)
print(json.dumps(result, indent=2, default=str))