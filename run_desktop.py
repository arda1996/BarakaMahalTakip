"""PyInstaller giris noktasi.

app/desktop.py paket ici goreli import kullaniyor; PyInstaller'a ise
paket disindan duz bir betik vermek gerekiyor. Bu dosya o koprudur.
"""

from app.desktop import main

if __name__ == "__main__":
    main()
