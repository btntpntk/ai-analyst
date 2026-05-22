import requests
import os
import logging

logger = logging.getLogger(__name__)

class SECTHMapper:
    _cache = {}

    @classmethod
    def get_company_data(cls, ticker: str, year: str = "2023"):
        # Normalize ticker (e.g., "BBL.BK" -> "BBL")
        clean_ticker = ticker.split('.')[0].strip().upper()
        
        if clean_ticker in cls._cache:
            return cls._cache[clean_ticker]
        
        headers = {'Ocp-Apim-Subscription-Key': os.getenv("SEC_TH_API_KEY")}

        # Try English ('E') then Thai ('T') as fallback [cite: 14, 15, 59]
        for lang in ["E", "T"]:
            url = f"https://api.sec.or.th/onereport/sbo/{year}/info/{lang}" 
            try:
                response = requests.get(url, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    for comp in data:
                        symbol = comp.get("symbol", "").split('.')[0].strip().upper() 
                        cls._cache[symbol] = {
                            "unique_id": comp.get('unique_id'),
                            "shares": comp.get('common_paidup_share', 0) 
                        }
                    if clean_ticker in cls._cache:
                        return cls._cache[clean_ticker]
            except Exception as e:
                logger.error(f"Mapping error: {e}")
        return None