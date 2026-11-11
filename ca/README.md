Intermediate certificates for FTPS servers that don't send them along
(publish.py loads every *.pem here on top of the system's trusted roots).

- `letsencrypt-gen-y.pem`: Let's Encrypt YE2 and ISRG Root YE cross-signed
  by ISRG Root X2 (from http://ye2.i.lencr.org/ and
  https://letsencrypt.org/certs/gen-y/root-ye-by-x2.pem). Needed for
  ftps.deitso.de. Checked with `openssl verify`: chains to ISRG Root X2.
  Valid until 2028.
