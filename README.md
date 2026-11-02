# Zucky Center

A single-file, standard-library Python prototype that models an underground / climate-sited AI data center as an energy ledger: sCO2 waste-heat generator, Carnot battery, liquid-air energy storage (LAES), district-heat sales with seasonal supply temperature and booster heat pump, climate-dependent PUE, and price-timed battery dispatch. It also prints its own design document, with every number computed live by the code.

**Prototype for exploration, not a design.** Every value marked ILLUSTRATIVE is an assumption, not a measurement; climate profiles are generic, not real weather data. Economics are not claimed.

> The ledger reports; it does not rule.

## Run (Python 3, no dependencies)

```
python3 zucky_center.py            # baseline vs evolved + tables
python3 zucky_center.py doc        # full design document (plain text)
python3 zucky_center.py doc --md   # same document as Markdown
python3 zucky_center.py --test     # all tests (432)
python3 zucky_center.py --json     # numbers behind the document
```

## License

CC0 1.0 Universal: dedicated to the public domain. Credited to humanity.
