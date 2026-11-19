# Pending Business Questions — D&T Hotel bot

> Auto-generated from a deep analysis of **1,080 real Instagram dialogues** (11,002 messages) between the hotel's human operators and clients.
>
> These are questions where the **correct business policy is genuinely uncertain** from the current spec/config — i.e. only the owner can decide. Questions already answered by `project_spec.md` / `CLAUDE.md` were filtered out (42 candidates dropped as already-known).
>
> **How to use:** answer under each `**Answer:**` line (Ukrainian or English is fine; exact numbers/ages where asked). Save the file, then tell me you're ready and I'll turn your answers into code, templates, and `pricing.json` changes with tests.

**Total questions:** 19  ·  High: 9  ·  Medium: 9  ·  Low: 1

---

## 🔴 HIGH PRIORITY

### 1. 10% loyalty / length-of-stay discount — is it real, automatic, and stackable?

*Category: `discounts` · seen in ~34 dialogues*

**Question:**

Operators in 30+ separate dialogues proactively offered a flat 10% discount for stays of 3+ nights ('програма лояльності'). Please confirm definitively:
(a) Is this a firm, automatable policy the bot should apply to EVERY quote of 3+ nights, or a discretionary upsell?
(b) Exact conditions — is it 3+ nights ALONE, or does it also require Instagram follow + a tagged photo/review (operators stated all three in some threads, only nights in others)? One operator (zasimskaa) implied it kicks in at 4 nights, not 3 — which is correct?
(c) Should the bot quote the pre-discount total and mention the discount, or quote the already-discounted total? Operators did both (applied upfront for some clients, 'отримаєте при поселенні' for others).
(d) Does it STACK with the УБД -20% military discount?

*Why this is uncertain:* This is the single most frequent theme across the entire dataset — operators offered it spontaneously and inconsistently (sometimes applied in the math, sometimes only mentioned, with 1-3 different conditions). The bot currently has zero knowledge of it, so it silently underquotes or fails to honour a discount real clients were promised. Directly money-affecting on nearly every multi-night booking.

*Example threads:* `alladuhlij_17877182748596492`, `elenaobernak_1458891428834197`, `rut_1294752926075199`, `kseniakovbasa_1509636203880544`

**Answer:**



---

### 2. Meal-plan bundle prices — what are the exact 2-meal and 3-meal per-person daily rates?

*Category: `food` · seen in ~30 dialogues*

**Question:**

The bot's FAQ uses breakfast 350 / lunch 450 / dinner 450 à la carte, but operators consistently quote DISCOUNTED bundles with wildly varying numbers: 2-meal = 700, 750, 750-800, or 800-850 грн; 3-meal = 950, 1000, 1000-1050, or 1050-1100 грн. Breakfast itself was quoted at 300 in several threads (not 350). Group 3-meal was lower (~950) than individual. Some operators only say 'надамо знижку, прорахуємо' without a number.
(a) What are the definitive per-person/day prices for breakfast-only, 2-meal (which combo?), and 3-meal, for each of June/July/August?
(b) Is breakfast 300 or 350?
(c) Is there a separate lower group meal rate? Do bundle prices vary by month (operator said August menu/prices only finalised late July)?
(d) Should the bot quote fixed numbers or ranges?

*Why this is uncertain:* Meal pricing appears in 30+ threads with no two operators agreeing on the bundle figures, and the à-la-carte sum (e.g. 800 for B+D) contradicts the quoted bundle (700). Clients ask about food on most multi-night bookings, so the bot needs one authoritative table.

*Example threads:* `alladuhlij_17877182748596492`, `turiukrainoutaevropa_2474785289651885`, `alisonka_18057683336694446`, `tetianatiurina_2206287333501561`

**Answer:**



---

### 3. Children pricing: public template (6-12=50%, 12-16=25%) vs internal code rule — which is authoritative?

*Category: `pricing` · seen in ~22 dialogues*

**Question:**

Operators repeatedly sent the public template verbatim to real clients: 'до 6 — безкоштовно (якщо не займають додаткового місця) / 6-12 — 50% знижка / 12-16 — 25% знижка'. The bot's internal rule is different: only guests BEYOND base capacity 2 pay, 0-6 free / 7-12 = дитяче_місце (half extra-bed rate) / >12 = додаткове_місце (full rate), with NO 25% tier. Please confirm the single authoritative rule:
(a) Is the 12-16 = 25% bracket real and to be coded, or does >12 = full rate?
(b) Is the 6-12 '50% знижка' off the ROOM rate (per child, regardless of occupancy) or 50% of the extra-bed surcharge (only beyond capacity 2)? These produce very different totals.
(c) Edge case at exactly age 6: free (per '0-6') or 50% (per '6-12')? And exactly 12: дитяче (half) or додаткове (full)?
(d) Operators consistently ask 'діти до 16 років?' implying 16 is the discount cutoff — is that the real boundary?

*Why this is uncertain:* This contradiction is confirmed in 20+ live dialogues where the public template was sent to paying clients, and it materially changes the total on every family quote. The two models compute different prices, and operators apply them inconsistently (some treated a 13/14-yr-old as a full adult, others quoted 25% off). Highest money-risk internal contradiction in the system.

*Example threads:* `olegkylik_1948017923258777`, `anastasiakrivenko_1696975571717417`, `varvaracvik_2083064816424417`, `tanyagritsay_1883553943035437`

**Answer:**



---

### 4. Напівлюкс base capacity and extra-person surcharge — flat +200 грн/person vs tiered rule

*Category: `pricing` · seen in ~22 dialogues*

**Question:**

Many operators described Напівлюкс as: 'двомісне 2700, можна до 5 осіб, кожна наступна особа + 200 грн' — i.e. base capacity 2 and a FLAT +200 грн/person/night for guests 3-5, all ages. This contradicts (a) the BRIEF's claim that Напівлюкс is a 3-person room, and (b) the internal tiered дитяче_місце/додаткове_місце extra-bed logic.
(a) For Напівлюкс, is the pricing base capacity 2 or 3?
(b) Is the extra-guest charge a flat +200 грн/person/night, or the age-tiered child/adult rate from pricing.json? Which is authoritative today?
(c) Same question for the 3rd person in a Стандарт — what is the exact per-night uplift (operators quoted Стандарт 3-person at ~2500-2700/night vs 2-person base)?

*Why this is uncertain:* The flat +200 figure appears in 20+ threads as a canned operator line, and it produces different totals than the bot's tiered engine. Combined with the base-capacity ambiguity (2 vs 3), every Напівлюкс/3+person quote is at risk of being wrong.

*Example threads:* `marinaanatolevna_18056088314693536`, `svitlana_1837948540927821`, `maksim_17868035181566668`, `kata_2008590483874526`

**Answer:**



---

### 5. Off-season pricing (April, May, September) — operators actively quoted concrete rates the bot refuses to give

*Category: `pricing` · seen in ~18 dialogues*

**Question:**

The bot treats anything outside June/July/August as 'off-season, price not set'. But operators quoted concrete rates repeatedly: MAY Стандарт від 1800 грн/night (Стандарт+ 2200/2400, Напівлюкс 2700-3000); SEPTEMBER Стандарт від 2000 грн (Стандарт+ 2400, Напівлюкс 3000) — and even firm totals (e.g. '5 ночей стандарт 2 особи 10000 грн'); APRIL Напівлюкс 2700/night; MARCH Стандарт 2000.
(a) Are May and September 2026 now priced — should they be added to pricing.json, and what are the exact per-night weekday/weekend rates by room type?
(b) If still off-season, what is the EXACT lead-capture response the bot should give (collect dates+party size and promise callback, or state 'prices not ready')?

*Why this is uncertain:* Operators quoted off-season prices in ~15 threads, often identical to the June baseline. The bot currently declines to quote, losing leads the human team converts. May and September are recurring requests. Either pricing.json needs extending or the bot needs a precise holding script.

*Example threads:* `momboss_1771395527577467`, `anastasiacykalo_953049414396190`, `natasadegaluk_2501744993621480`, `krasnopolska_2027743645289822`

**Answer:**



---

### 6. Per-person group pricing — is there an official rate table, and where does the 20+ redirect actually apply?

*Category: `pricing` · seen in ~17 dialogues*

**Question:**

For groups, operators quoted a PER-PERSON nightly rate (not per-room): roughly 750 грн (May), 800 (June), 825 (July), 850 (Aug) at 3-5 per room; some threads showed length-of-stay scaling (850/825/800 for 1/2/3 nights). School/children groups were quoted ~750 грн/person.
(a) Is there an official per-person group rate table by month/occupancy the bot (or Yaroslav) should use? Please provide exact figures.
(b) Crucially: the bot is told to redirect ALL 20+ guests to Yaroslav, but operators FULLY quoted and even booked groups of 21-30 in-chat (zhurbitsssa: 21 ppl, 46,400 грн, took 25,000 deposit). Should the bot hard-redirect at 20+, or only redirect EVENTS (weddings/banquets) while quoting accommodation-only groups? What is the real threshold?
(c) What is the actual hotel capacity (one operator said 50, another said 'can host 30')?

*Why this is uncertain:* Per-person group pricing appears in 15+ threads and directly contradicts both the per-room model and the 20+ redirect rule — operators routinely transacted large groups themselves. This is a high-value B2B segment the bot currently mishandles by redirecting business the team would have closed.

*Example threads:* `olga_17952571434135093`, `bejdavladislav_1742741740443098`, `zhurbitsssa_1557135719146889`, `martagiblak_1605453620839006`

**Answer:**



---

### 7. УБД -20% scope — whole family/booking or veteran's room only? Apply upfront or at check-in?

*Category: `discounts` · seen in ~15 dialogues*

**Question:**

Operators consistently described the discount as 'для УБД ТА ЇХ СІМЕЙ' / 'для родини військового' / '1 ступінь рідства' — implying it covers the whole family booking, not just one room. They also handled timing inconsistently: some applied -20% upfront in the quote, others quoted full price and said 'надамо при поселенні при наданні документів'. Some also required an Instagram follow.
(a) Does -20% apply to the ENTIRE booking (all rooms) for a veteran's family, or only the room the veteran occupies?
(b) Does it extend to 1st-degree relatives / families of fallen soldiers even when the veteran is not personally staying? Which documents qualify?
(c) Should the bot apply the discount in the quoted price, or quote full and note check-in verification?
(d) Is an Instagram follow actually required? Does 'знижки не сумуються' mean it can't combine with the loyalty discount?

*Why this is uncertain:* The BRIEF only says '-20% on that room'. Real operators applied it to whole families, to relatives of the fallen, with varying document/Instagram conditions, and at different timing. This affects multi-room family bookings (the most common veteran case) and the quoted total.

*Example threads:* `nataopalko_1003963532169339`, `rudenkotatana_1553928282671607`, `nataliastepanuk_17972969445043594`, `juliyakomisaruk_2028978358503031`

**Answer:**



---

### 8. Single-occupancy (solo) pricing — discounted rate or full room rate?

*Category: `pricing` · seen in ~13 dialogues*

**Question:**

Operators handled solo guests inconsistently. Some quoted a clearly REDUCED rate: Стандарт 1500/night (June) or 1900/night, vs the 2-person base of 2000-2200; 'вартість буде нижче бо ви проживаєте одна'. Others quoted the SAME full 2-person rate for 1 person ('вартість не змінюється'; 7600 для 1 особи = same as 2).
(a) Is there an official single-occupancy rate, and if so what is it per room type/month (a fixed delta, e.g. -300/night, or a %)?
(b) Or does a solo guest always pay the full 2-person room price? The bot needs one deterministic rule.

*Why this is uncertain:* Solo bookings are common and operators split roughly 50/50 between quoting a discount and quoting full price — a direct internal contradiction. The bot has no solo rule at all, so it can't quote consistently.

*Example threads:* `annazima_18065540579693536`, `veronikaorlovamelnicenko_2422784658208191`, `olegkapustin_1920874112648384`, `milanakristal_2043573620368794`

**Answer:**



---

### 9. Cancellation refund window — 7 days or 10 days? (template contradiction)

*Category: `policy` · seen in ~12 dialogues*

**Question:**

The deposit/IBAN template was sent to paying clients in two conflicting versions: '≥10 днів до заїзду' and '≥7 днів години до заїзду' (the latter also appears garbled, suggesting an outdated/corrupted template still in circulation). The BRIEF says 10 days.
Please confirm the single authoritative free-cancellation deadline (7 or 10 days before arrival) so the corrupted 7-day template can be purged everywhere.

*Why this is uncertain:* Both versions were sent verbatim to real paying clients across many threads — a live contradiction in a legally/financially binding term. Even though the BRIEF says 10, the residual 7-day template is a real risk and must be confirmed and eliminated.

*Example threads:* `rut_1294752926075199`, `svetlanamakhorina_18062034230693536`, `natasaprocenko_1920605702661413`, `stanislavsander_1977975576930534`

**Answer:**



---

## 🟡 MEDIUM PRIORITY

### 10. Missing amenity FAQs: fridge-by-room-type, cribs/playpens, children's playground/pool, kids' room, entertainment room, conference hall, SPA, cottages, what's-included list

*Category: `rooms` · seen in ~18 dialogues*

**Question:**

Several recurring amenity questions have no bot template and inconsistent operator answers. Please confirm each so templates can be built:
(a) FRIDGE: do Стандарт+ and Напівлюкс have a fridge (and Стандарт not)? Operators added 'з холодильником' inconsistently; a returning guest complained Стандарт still has none. Should it be a differentiating amenity in the room template?
(b) CRIBS/PLAYPENS: confirmed NOT available ('такого поки не маємо') — should the bot state this?
(c) CHILDREN: no kids' playground ('ще немає'), no children's menu, no children's pool ('поки немає, на липень буде'?) — but there IS an indoor entertainment room (projector, table tennis, chess) and volleyball/football pitches. What is the accurate current state?
(d) CONFERENCE/EVENT HALL: exists (quoted 1000 грн/evening; included for some groups) — should the bot mention it for corporate/retreat enquiries?
(e) SPA: 'в процесі будівництва' — mention as upcoming or stay silent?
(f) COTTAGES / two-bedroom units: none ('у нас готель') — should the bot say so and pivot to 2 rooms?
(g) Should the 'what's included' template add river access, table tennis, badminton, chess, нарди?

*Why this is uncertain:* These are factual amenity questions the bot will receive and currently has no answer for, leading to silence or invented answers. Operators answered inconsistently or not at all. Each needs a one-line confirmed template; bundled here because individually low-volume but collectively frequent.

*Example threads:* `oksanastahovskaa_1363906078965136`, `irinaholavcuk_1802573367795785`, `natalia_1037747845378489`, `mariachupirchuk_18060899105693536`

**Answer:**



---

### 11. Family splitting / multi-room recommendation logic and room-config sub-types

*Category: `rooms` · seen in ~16 dialogues*

**Question:**

Operators frequently and proactively offered '2× Стандарт' vs '1 Напівлюкс' as parallel priced options, sometimes recommending the split ('зазвичай діти в 1 номері, батьки в іншому'), and exposed internal sub-types by name with bed configs and DIFFERENT prices (Стандарт сімейний тип 1/тип 2, Стандарт+ сімейний), even noting a surcharge for a double/king bed ('двоспальне — трішки дорожче').
(a) When a family of 4-5 fits in one room but children are teens, should the bot proactively offer the 2-room option, and which should be 'recommended'? At what composition should it suggest splitting?
(b) Should the bot expose 'Стандарт сімейний' / bed-config sub-types when a family needs a specific layout, or keep folding into 3 names? If folded, is there a fixed surcharge (грн) for the сімейний/double-bed config, and is it in pricing.json?
(c) For multi-family / multi-party requests with unspecified room distribution, should the bot ask who sleeps where before quoting?

*Why this is uncertain:* Multi-room/family-split handling appears in 15+ threads; operators both expose sub-types and recommend splits in ways the bot is told NOT to do (fold into 3 names). There's a real bed-config surcharge being charged that the bot can't see. Affects quoting accuracy and conversion for the most common family scenario.

*Example threads:* `valentinacertok_1509571334046556`, `sergejgerasimenko_1825282338857781`, `vitalia_1885039822884658`, `nazarmironcuk_875641024931389`

**Answer:**



---

### 12. Partner activities (чани/sauna, jeep, ATV, rafting, horses, tennis) — quote prices or just refer?

*Category: `activities` · seen in ~15 dialogues*

**Question:**

Clients frequently ask for activity prices. Operators were inconsistent: some refused ('партнери, ціни не підкажу'), some quoted (чан ~2000, джип-тури від 3500, сауна ~1200, теніс безкоштовно), some offered to arrange transfer to a partner чан complex.
(a) Should the bot quote indicative prices, or always say 'партнери, допоможемо при поселенні'?
(b) If quoting: provide current starting prices for чан, сауна, джип-тури, рафтинг, квадроцикли, коні.
(c) Is tennis always free for guests (deposit for equipment)? Is there a booking contact/link?

*Why this is uncertain:* Activities come up in 15+ threads; the bot has only a generic 'these exist' line while clients want prices before arrival. Inconsistent operator handling means no deterministic answer exists. Affects upsell revenue and pre-booking confidence.

*Example threads:* `olga_17952571434135093`, `marina_828668550322999`, `allanesteadvokatzemelnisimejnispadsina_1838857194173765`, `svetlanamakhorina_18062034230693536`

**Answer:**



---

### 13. Blogger/influencer barter, PR collabs, and legitimate B2B partners — engage or ignore?

*Category: `policy` · seen in ~15 dialogues*

**Question:**

The BRIEF says ignore B2B/marketing spam silently. But the hotel ACTIVELY runs barter deals — operators (and the hotel proactively) negotiated free stays for photographers/bloggers/videographers (up to 3 nights for 2 Reels+post+stories), offered a singer a promo code ('ФЕДИШИН', 10% to followers), engaged a Visit Ukraine affiliate partner (gave email hotel.dt2024@gmail.com), and a tea supplier.
(a) Should the bot distinguish genuine collab/partner/influencer outreach from spam, and route it to a human (who — Yaroslav, an SMM manager, the hotel email)?
(b) What is the standard barter offer that could be templated (nights, room type, deliverables)?
(c) Are there active promo codes the bot should recognise/honour?

*Why this is uncertain:* Barter/collab outreach appears in 15+ threads and the hotel clearly WANTS these deals (often initiating them), yet the bot's rule would silently ignore them — losing marketing value. Needs a routing decision and possibly a promo-code list.

*Example threads:* `valeriiafotografhmelnickij_1988321482569714`, `streetfestua_1463201045820158`, `veducijdmitrokalasnikov_1975465883855067`, `maria__kosylo_1657004485683773`

**Answer:**



---

### 14. Check-in / check-out times and early/late policies — no bot template exists

*Category: `logistics` · seen in ~12 dialogues*

**Question:**

Clients ask this constantly; operators answered 14:00 check-in / 12:00 check-out, with nuances: early arrival welcomed ('з території не виганяємо, можете прийти зранку в басейн/бар'), early check-in free if room is ready, late check-out 'можна домовитись' (no price given). One client arrived at 10:00 to a closed door.
(a) Confirm official times (14:00 / 12:00).
(b) Is early check-in free if the room is ready, or is there a fee?
(c) Is late check-out free/negotiable or a fixed fee?
(d) What phone number should the bot give for late-night arrival (after 22:00) or someone who arrives to no one at reception?
Please provide template text.

*Why this is uncertain:* Asked in 10+ threads with no bot template, forcing silence or improvisation. The 'arrived at 10:00, door closed' incident shows real friction. Low money impact but high frequency and easy to template once confirmed.

*Example threads:* `rut_1294752926075199`, `dariakramarenko_2001794863794827`, `mart_18064141418693536`, `tetiana_k_1014327138211964`

**Answer:**



---

### 15. Booking holds / waitlist for sold-out dates, deposit deadline, deposit floor, and group/short-stay deposit %

*Category: `policy` · seen in ~11 dialogues*

**Question:**

Several deposit/hold practices appear in real dialogues that the bot has no rule for:
(a) WAITLIST/HOLD: operators offered informal holds ('будемо мати на увазі', 'тримати бронь?') and even no-deposit blocks until a deadline ('до 12 травня до 12:00'). Should the bot offer to waitlist clients when dates are sold out, and offer complimentary holds? For how long and what data to collect?
(b) DEPOSIT DEADLINE after IBAN: operators said 24h but extended on request. What is the official window (24/48/72h) and can the bot extend it?
(c) DEPOSIT FLOOR: a fixed minimum deposit (e.g. 'мін сума 4900 грн', or '10 000 грн' for groups) was quoted independent of the 50% rule. Is there a floor?
(d) DEPOSIT %: the BRIEF says 50%, but operators offered 20% (one Напівлюкс booking) and 30% (groups) and occasionally demanded 100% upfront. What is the real rule by booking type/season?

*Why this is uncertain:* These payment-mechanics variations appear across 10+ threads and contradict the BRIEF's flat 50% rule. Holds/waitlists are a recurring lost-lead scenario. Money-and-trust-affecting, but lower frequency than the top items.

*Example threads:* `anastasianefedova_18064111736693536`, `anaskacko_803568956024816`, `valentinazdorovabiznesfitline_1780066313401740`, `nikoletta_1900739741320426`

**Answer:**



---

### 16. Coliba/restaurant operating constraints and non-resident dining

*Category: `food` · seen in ~8 dialogues*

**Question:**

The food FAQ presents the коliba as a reliable on-site facility, but operators revealed constraints: it operates only above a minimum guest count (variously stated as '>10 осіб' or '>15 осіб'; otherwise delivery), food is described as 'партнери, 50 м від готелю' in some threads vs 'на території' in others, and non-residents can dine only 'по передзамовленню' with a minimum group size.
(a) Does the coliba have a minimum occupancy to operate (10 or 15 guests)? What should the bot say about food when occupancy is low — delivery only?
(b) Is the coliba on-site or a partner 50m away?
(c) Can non-residents book it (pre-order, minimum size)? Is Yaroslav (0673445220) the food contact, or a separate one?

*Why this is uncertain:* The bot presents food as always-available, but operators repeatedly caveated availability and location — risking client disappointment on smaller bookings. The on-site vs 50m-partner discrepancy and non-resident dining are recurring gaps.

*Example threads:* `sonyarich_1894345251968216`, `rudenkotatana_1553928282671607`, `zvln_yann_18076671191577723`, `elizavetasergiivna_2018677546195622`

**Answer:**



---

### 17. Pet surcharge amount and scope (300 грн), and cats

*Category: `policy` · seen in ~7 dialogues*

**Question:**

The pets FAQ states 'up to 10kg, vet passport' but no fee. Operators quoted a fee in multiple threads: '300 грн за добу' (per night) in some, '300 грн за весь період' (per stay) in another — directly contradictory. Cats were asked about but the BRIEF/template only mention dogs.
(a) Is the pet fee 300 грн per NIGHT or per STAY? Is 300 the canonical figure?
(b) Does it apply to all pets under 10kg or vary by size?
(c) Are cats accepted under the same conditions? Should the template say so?

*Why this is uncertain:* The fee is quoted in several threads but per-night vs per-stay contradicts within the dataset, and the bot currently quotes no fee at all (under-informing clients). Cats are a recurring gap. Money-affecting per booking with a pet.

*Example threads:* `elenaobernak_1458891428834197`, `oleckaoriskina_18070304456124339`, `urijvengrin_1006876621799099`, `maks_1025746913129803`

**Answer:**



---

### 18. Transfer from Vorokhta — does it exist, what is the real price, and is there a hotel car?

*Category: `logistics` · seen in ~6 dialogues*

**Question:**

The bot's FAQ says ~1000 грн from Vorokhta, but operators contradicted this badly: one said 'трансферу немає', another 'залежить від ціни на паливо', another quoted 'автобусом 500 грн, легковою 300 грн', and several mentioned the hotel's OWN car 'інколи за оплату' or 'відправимо авто' (possibly free).
(a) Is there a transfer at all, and is it the hotel's own vehicle or a third party (and is the hotel car currently operational)?
(b) What is the real price — flat ~1000, or 300-500 by vehicle type?
(c) Should the bot quote a fixed figure, a range, or 'confirmed before arrival'?

*Why this is uncertain:* The single FAQ figure (~1000) is contradicted by at least four different operator answers including 'no transfer' and '300 грн'. The bot is currently quoting a number that may be wrong by 2-3x. Affects every 'how do I get there' conversation.

*Example threads:* `valentinacertok_1509571334046556`, `marina_828668550322999`, `viktoriyastepnaminenko_2009341887126906`, `alisonka_18057683336694446`

**Answer:**



---

## ⚪ LOWER PRIORITY

### 19. Business/fiscal documents, OTA modifications, and other one-off operational policies

*Category: `policy` · seen in ~7 dialogues*

**Question:**

Lower-frequency but real situations with no bot policy:
(a) FISCAL DOCS: workcation/UBD clients requested рахунок, акт наданих послуг, фіскальний чек (with stamp) for employer/state reimbursement — operators confirmed these are provided, payment by IBAN. Should the bot confirm this capability proactively for business/UBD clients?
(b) OTA SWAP: a Booking.com guest asked to swap rooms via Instagram — operator handled a manual price reconciliation + Booking cancellation. Should the bot attempt this or always hand off?
(c) MINOR BIRTH CERTIFICATES: operators told group/school clients to bring children's birth-certificate copies at check-in. Mandatory for all bookings with minors, or only groups? Mention proactively?
(d) ACCESSIBILITY: a family with a child with cerebral palsy got a standard quote — does the hotel have any accessible rooms/facilities, and should the bot flag disability mentions to a human?
(e) FORCE-MAJEURE REFUNDS: an operator had no documented refund process and offered to convert deposit to future-stay credit — is that a standing option?

*Why this is uncertain:* Each is individually rare but represents a real situation the bot mishandled (silence or improvisation). Bundled as a single 'operational policy gaps' question for efficiency; the fiscal-document and accessibility points in particular carry reputational/legal weight.

*Example threads:* `alisonka_18057683336694446`, `anaskacko_803568956024816`, `olga_17952571434135093`, `malenkijgvintik_1873472409995268`

**Answer:**



---

---
