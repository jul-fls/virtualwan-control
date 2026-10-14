"""HTTP-only production server; TLS is terminated by the operator's proxy."""
import ipaddress
import logging
import os
from urllib.parse import urlparse
from waitress import serve
from .web import create_app


def main():
    logging.basicConfig(level=logging.INFO)
    app=create_app()
    # This only describes the public URL to WSGI. The socket remains plain HTTP.
    options={'host':'0.0.0.0','port':8080,'threads':8,
             'url_scheme':urlparse(os.environ.get('VWAN_PUBLIC_URL','http://localhost:8080')).scheme,
             'max_request_body_size':1024*1024}
    proxy=os.environ.get('VWAN_TRUSTED_PROXY','').strip()
    if proxy:
        # Never trust arbitrary clients or a wildcard for forwarding headers.
        options.update(trusted_proxy=str(ipaddress.ip_address(proxy)),trusted_proxy_count=1,
                       trusted_proxy_headers={'x-forwarded-for'})
    serve(app,**options)


if __name__=='__main__':main()
