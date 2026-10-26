Read AGENTS.md — it is the guide for this app.
Core owns every signing/confirmation surface: use core words, isolate addon text, and show signed values in full through `src/components/sign/visible.tsx`; agents never sign.
Keep every page usable at 720 px beside the terminal dock on a 13" notebook; run `npm run layout:guard -- --url http://localhost:<port> --docks min,max` without piping against your dev server.
