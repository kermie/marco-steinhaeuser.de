Intermediate certificates for FTPS servers that don't send them along
(publish.py loads every *.pem here on top of the system's trusted roots).

- `letsencrypt-gen-y.pem`: all Let's Encrypt Gen-Y intermediates (YE1-YE3,
  YR1-YR3, from http://<name>.i.lencr.org/) plus ISRG Root YE cross-signed
  by ISRG Root X2 and ISRG Root YR cross-signed by ISRG Root X1 (from
  https://letsencrypt.org/certs/gen-y/). Needed for ftps.deitso.de, which
  sends only its own certificate. Let's Encrypt picks one of these at
  random at each renewal (Nov 2026: YE2 -> YE1 broke publishing), so all
  are bundled. Checked with `openssl verify`: each chains to ISRG Root X1
  or X2. Valid until 2028 -- the next Let's Encrypt generation needs a new
  bundle before then.
