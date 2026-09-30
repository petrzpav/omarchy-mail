"""The demo inbox: a morning of mail for Mia Hart at a small web studio. Everyone is made up."""

from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

ME = ("Mia Hart", "mia@northwind.example")

# (minutes ago, from name, from address, subject, body, unread)
MAIL = [
    (8, "Jonas Berg", "jonas@alderbrew.example", "Website relaunch: a few things before Friday", """\
Hi Mia,

thanks for the preview link, the new homepage looks great and the team loves the photos.

A few things before we sign off:

Can we move the launch from October 7 to October 14? Our new menu is not printed yet.

Will the contact form send to both me and Clara, or only to the shared info@ address?

What would a Czech version of the site cost, roughly?

And could you please send the updated invoice with the extra photo day on it?

Thanks a lot,
Jonas
""", True),
    (21, "GitHub", "notifications@github.example", "[northwind/alderbrew-site] Run failed: deploy on main", """\
The workflow "deploy" failed on main (commit 4f2c9e1).

  build   passed
  test    passed
  deploy  failed: SSH key for staging.northwind.example was rejected

View the run to see the full log.
""", True),
    (34, "Ledgerly", "billing@ledgerly.example", "Invoice INV-2026-0931 from Brightpixel Hosting", """\
Brightpixel Hosting sent you an invoice.

Invoice: INV-2026-0931
Amount due: EUR 39.00
Due: October 14, 2026
Service: VPS L, October 2026

Pay online or by bank transfer to the account on the invoice.
""", True),
    (52, "Cloudbox", "no-reply@cloudbox.example", "Your sign-in code is 482 913", """\
Use this code to finish signing in to Cloudbox:

482 913

It expires in 10 minutes. If this wasn't you, change your password.
""", True),
    (75, "Eva Kral", "eva@northwind.example", "Notes from the Alderbrew call", """\
Hey Mia,

quick notes from today's call with Jonas and Clara:

- they want bigger photos on the menu page
- Clara will send the final opening hours by Thursday
- we owe them a quote for the Czech translation

I put the tasks on the board. Talk tomorrow!
Eva
""", True),
    (96, "Design Weekly", "hello@designweekly.example", "Design Weekly #212: variable fonts everywhere", """\
This week in Design Weekly:

- Variable fonts are finally everywhere: 7 sites doing it well
- The return of the grid
- Sponsor: 30% off all templates this week only

Unsubscribe | View in browser
""", False),
    (140, "SkyLine", "booking@skyline.example", "Your booking PRG to LIS is confirmed", """\
Booking reference: K7Q2PX

Prague (PRG) to Lisbon (LIS)
Thursday October 22, 2026, 07:15 to 09:50

Passenger: Mia Hart
Check-in opens 24 hours before departure.
""", False),
    (190, "Parcelo", "tracking@parcelo.example", "Your order is on its way", """\
Good news! Your order #58213 from Deskshop has shipped.

Items: Standing desk frame, oak top 140 cm
Estimated delivery: Friday, October 2

Track your parcel in the Parcelo app.
""", False),
    (240, "Kanbo", "notify@kanbo.example", "Eva mentioned you on \"Czech translation quote\"", """\
Eva Kral mentioned you in a comment on the card "Czech translation quote"
on the board Alderbrew relaunch:

"@Mia can you estimate this before Friday?"

Reply in Kanbo
""", False),
    (300, "Mom", "hana.hart@mailbox.example", "Sunday lunch?", """\
Hi sweetie,

are you coming on Sunday? Dad is making his svíčková. Bring Tom if he's free.

Love, Mom
""", False),
    (420, "Northwind contact form", "wordpress@northwind.example", "New inquiry: website for a bakery", """\
New message from the contact form on northwind.example

Name: Petra Novak
Email: petra@kvasbakery.example
Message: Hi, we are opening a small bakery in Karlin and need a simple website
with opening hours and a menu. Do you have time in November?
""", False),
]


def messages(now: datetime | None = None) -> list[tuple[bytes, bool]]:
    now = now or datetime.now().astimezone()
    out = []
    for ago, name, addr, subject, body, unread in MAIL:
        m = EmailMessage()
        m["From"] = f"{name} <{addr}>"
        m["To"] = f"{ME[0]} <{ME[1]}>"
        m["Subject"] = subject
        m["Date"] = format_datetime(now - timedelta(minutes=ago))
        m["Message-ID"] = make_msgid(domain=addr.split("@")[1])
        m.set_content(body)
        out.append((m.as_bytes(), unread))
    return out
