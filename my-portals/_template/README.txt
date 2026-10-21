How to add your own portal
============================
1. Copy this folder:
     cp -r my-portals/_template my-portals/my-cafe
2. Edit my-portals/my-cafe/index.html (keep this form exactly):
     <form method="POST" action="/auth"> ... <button>Connect</button> </form>
3. Optional: edit style.css, add images. Keep it small (inline CSS best).
4. Run and pick it:
     sudo ./run.sh   -> choose my-cafe from the portal list
   Or switch while running: press [s].

Files the server understands:
  index.html   (recommended entry; else login.html, manifest entry, or first *.html)
  success.html (optional)  shown after clicking Connect
  anything else (css, js, images) served statically.

Forms: POST anywhere (/auth, /login, login.php, ...) authorizes the
client's MAC and redirects to /success. Typed fields are logged to
run/submissions.log. Unknown GET paths redirect to /.
Probes like /generate_204 and /hotspot-detect.html auto-redirect to /.
