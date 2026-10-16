"""Text from external tools, including legacy Perl diagnostics; standalone-safe."""
def decode_external_output(raw):
    """pvesh progress/diagnostics can mix UTF-8 with Perl Latin-1 bytes.

    Keep valid UTF-8 intact, and map only undecodable bytes to their Latin-1
    characters. Replacing or ignoring them would corrupt accented VM notes.
    """
    if isinstance(raw,str):return raw
    try:return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('utf-8',errors='surrogateescape').translate({0xdc00+b:b for b in range(128,256)})


