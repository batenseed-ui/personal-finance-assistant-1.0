# Personal Finance Assistant — Neon Glass UI

## Changes
- Login welcome splash uses `static/img/splash.png` (the supplied Personal Finance Assistant neon-wave image).
- Each welcome visit randomly selects a financial/investing motto and displays its attribution.
- The login form and all authenticated pages use the supplied blue-violet nebula background (`static/img/background.png`).
- Main app pages use translucent cards, fields, panels, and navigation with cyan/violet accents.
- Existing Flask routes, forms, and JavaScript behavior are preserved.

## Run
Install Flask and any dependencies your original project uses, then run:
```bash
python app.py
```
Open http://127.0.0.1:5000

Note: `rules.html` references `static/rules.js`, which was not included among the uploaded files. Add your existing `rules.js` to `static/` if you use the Rules and Scores page.


### Profile setup
After login, users complete a profile with their name, age, gender, profession, marital status, health insurance, and life/income protection. These protection answers are saved per account and are no longer asked on the main finance form.


### Visual assets
- `static/img/splash.png` — professional welcome-screen background (3840 × 2160).
- `static/img/background.png` — blue-violet application-page background (3840 × 2160).
- Insurance controls on the profile setup page use aligned custom checkboxes and matching cyan/violet focus and selected states.
