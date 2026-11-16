# Buy Me a Coffee Shop — five unpublished listing drafts

**Status: DRAFT ONLY. Do not publish, accept payment or enable the Quiz
checkout.** BMC currently shows the Shop as unlisted and requests identity
verification. We also need written clarification from BMC about virtual game
currency used in a quiz with a wagered bonus question. Read the section below
before proceeding.

These are **five independent digital Shop Extras**, not a coffee donation,
membership level, coupon or physical shipment. Bronze stars are delivered
to the customer's verified AI Quiz Game account following a signed provider
purchase notification. The customer must use the same email in both services.

## Product settings and copy

For each product choose **Start from scratch** / digital offering, and set:
- Price: the regular amount below, in USD.
- Advanced settings -> **Special price for members**: enabled, with the
  corresponding member price.
- **Allow buyer to choose a quantity**: OFF (one package per checkout).
- Product quantity/slots limit: OFF; ask-a-question: OFF unless a field is
  needed to confirm the customer's game email.
- Success page: **Confirmation message**, not a redirect claiming that the
  stars have already been credited.
- Mark **Save as draft**. Do **not** press Publish.
- Keep the actual BMC Extra ID and checkout URL private until entered into
  the production server configuration.
- Use the same description structure for all five packs.

| Listing title | Bronze stars | Regular USD | Members USD |
| --- | ---: | ---: | ---: |
| AI Quiz Game — 30 Bronze Stars | 30 | $3.00 | $2.40 |
| AI Quiz Game — 100 Bronze Stars | 100 | $9.00 | $7.20 |
| AI Quiz Game — 250 Bronze Stars | 250 | $20.00 | $16.00 |
| AI Quiz Game — 500 Bronze Stars | 500 | $35.00 | $28.00 |
| AI Quiz Game — 1000 Bronze Stars | 1000 | $60.00 | $48.00 |

### Description template

**AI Quiz Game — {N} Bronze Stars**

Add **{N} Bronze Stars** to your AI Quiz Game account at
https://squizthebot.com/.

Bronze Stars are digital in-game credits you can spend on selected quiz
features and exchange for higher-tier stars according to the game's rules.
These credits work only in AI Quiz Game. They cannot be withdrawn as cash,
transferred between players or exchanged for money or goods outside the
game. Some game features, including the bonus round, use other star tiers.

**Important:** Use the same verified email address at Buy Me a Coffee
checkout as in your AI Quiz Game account. After payment, your stars are
added automatically once the payment is verified. The credit may not appear
immediately; return to the game and refresh your star balance.

This is a digital game-credit purchase, **not** a general coffee donation
or a BMC membership. Refunds and credits are processed under the applicable
platform rules. If the credit is delayed, contact the creator with your BMC
purchase receipt; never send a password.

**Compliance note:** This description must not be published until BMC
confirms that digital game credits are acceptable given AI Quiz Game's
optional bonus round, which consumes a gold star and may award an in-game
platinum star. We should not omit this aspect from the approval request.

### Success page (same for every product)

Thank you for your purchase!

Your Bronze Stars will be added to your AI Quiz Game account once your
payment is verified. Please make sure the email used at checkout matches
your verified AI Quiz Game email.

Return to https://squizthebot.com/ and refresh your star balance.
If the stars do not appear after processing, contact the creator with your
purchase receipt.

### Internal category and visuals

If a category is required, prefer **Digital goods** / **Games**, whichever
BMC offers; do not misclassify these as physical products, donations,
investments or transferable tokens. An optional image can show a bronze
star and number of stars, but should not suggest cash conversion.

## Draft to BMC policy support — NOT SENT

**To:** support@buymeacoffee.com

**Subject:** Request for policy approval — in-game stars for an educational quiz

Hello Buy Me a Coffee Support Team,

I run AI Quiz Game (https://squizthebot.com/) and would like to confirm
whether a product I am preparing in my Buy Me a Coffee Shop is permitted
under your Community Guidelines, Shop Product Guidelines and payment
partner restrictions.

I plan to sell five fixed-price packages of virtual Bronze Stars
(30, 100, 250, 500 and 1,000 stars, priced at $3, $9, $20, $35 and $60).
The stars are credits used only inside my own educational quiz game for
features such as hints and category changes. They are non-transferable,
cannot be cashed out, have no value outside the game and are not crypto
tokens, NFTs or gift cards. Delivery would be automatic to a game account
after receiving a verified BMC Shop purchase webhook.

One detail may be relevant to your gambling restrictions: players can
use in-game currency to convert stars between tiers. An optional
sixteenth quiz question requires wagering one gold star; if a player
answers it successfully, they receive a platinum star. If they answer
incorrectly, the gold stake is lost. Neither the stake nor the prize
can be converted to real money or transferred to other users. Purchased
Bronze Stars can indirectly be converted into gold stars.

Could you confirm in writing:
1. Whether selling these non-cash, non-transferable in-game credits is
   allowed in Buy Me a Coffee Shop;
2. Whether the optional bonus question and star stake make the product
   prohibited gambling or otherwise restricted;
3. Whether automatic delivery to an external game account through a
   signed webhook is acceptable for a digital Shop Extra.

I have kept the Shop items unpublished pending your clarification and
would be happy to provide screenshots, game rules and the technical
delivery flow.

Thank you for your guidance.

Best regards,
AI Quiz Game creator

## Release gate

Do not enable BMC_STAR_SHOP_ENABLED until all are true:
- [ ] BMC has explicitly cleared the star product and bonus-stake mechanic,
      or the product/game has been modified to comply.
- [ ] Creator identity and Shop publication eligibility are verified.
- [ ] Five actual items and member prices are set and checked.
- [ ] Independent Shop webhook, live event schema and credentials verified.
- [ ] PostgreSQL backup + migration 0018 completed.
- [ ] Backend/frontend CI tests have passed.
- [ ] One controlled order, successful credit, replay and refund are verified.

See docs/BRONZE_STAR_SHOP.md for the engineering activation procedure.
