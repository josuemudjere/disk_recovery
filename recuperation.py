"""
Recherche et récupération de fichiers sur une clé USB ou une image de sauvegarde.

Tout se fait en LECTURE SEULE : rien n'est jamais écrit sur la source.

1. Recherche rapide (FAT32) : relit le système de fichiers, retrouve les fichiers
   présents ET supprimés avec leurs noms et dossiers. Si le secteur de démarrage
   est abîmé, utilise sa copie de secours (secteur 6 de la partition).
2. Recherche approfondie : parcourt toute la surface et reconnaît les fichiers
   d'après leur format (photos, documents, vidéos, sons, archives), même quand le
   système de fichiers est détruit. Les noms d'origine sont alors perdus.
"""

import array
import os
import re
import shutil
import struct
import sys
import time
from dataclasses import dataclass

import app

MIO = 1024 * 1024
MARGE_LIBRE = 1024 * MIO     # on s'arrête avant de remplir le disque de destination


# --------------------------------------------------------------------------- #
# Lecture de la source (clé ou image), tolérante aux secteurs illisibles
# --------------------------------------------------------------------------- #

class Lecteur:
    """Lecture seule, alignée sur les secteurs, avec un petit cache."""

    TAILLE_CACHE = 1 * MIO

    def __init__(self, fd, taille, bloc=512):
        self.fd, self.taille, self.bloc = fd, taille, bloc
        self.secteurs_illisibles = 0
        self._cache_pos, self._cache = -1, b""

    def _brut(self, debut, fin):
        """Lit [debut, fin) aligné sur les secteurs ; les secteurs illisibles valent zéro."""
        try:
            data = os.pread(self.fd, fin - debut, debut)
            if len(data) == fin - debut:
                return data
        except OSError:
            pass
        buf = bytearray()
        for s in range(debut, fin, self.bloc):
            n = min(self.bloc, fin - s)
            try:
                d = os.pread(self.fd, n, s)
            except OSError:
                d = b""
                self.secteurs_illisibles += 1
            buf += d.ljust(n, b"\0")
        return bytes(buf)

    def lire(self, pos, n):
        if pos < 0 or pos >= self.taille or n <= 0:
            return b""
        n = min(n, self.taille - pos)
        c = self._cache_pos
        if c <= pos and pos + n <= c + len(self._cache):
            return self._cache[pos - c:pos - c + n]
        debut = pos - pos % self.bloc
        if n <= self.TAILLE_CACHE // 2:
            fin = min(debut + self.TAILLE_CACHE, self.taille)
            self._cache_pos, self._cache = debut, self._brut(debut, fin)
            return self._cache[pos - debut:pos - debut + n]
        fin = pos + n
        fin = min(fin + (-fin) % self.bloc, self.taille)
        return self._brut(debut, fin)[pos - debut:pos - debut + n]

    def fermer(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def ouvrir_source(chemin, taille=None, bloc=512):
    """Ouvre une clé (/dev/rdiskN, mot de passe demandé) ou une image, en lecture seule."""
    if chemin.startswith("/dev/"):
        fd = app.ouvrir_avec_autorisation(chemin)
    else:
        fd = os.open(chemin, os.O_RDONLY)
        taille = taille or os.path.getsize(chemin)
    return Lecteur(fd, taille, bloc)


# --------------------------------------------------------------------------- #
# Utilitaires de sortie
# --------------------------------------------------------------------------- #

def nom_sur(nom):
    """Nettoie un nom de fichier venant de la clé."""
    nom = "".join("_" if (ord(ch) < 32 or ch in '/\\:') else ch for ch in nom).strip(" ")
    return nom if nom not in ("", ".", "..") else "sans_nom"


# Fichiers techniques sans intérêt pour l'utilisateur (créés par macOS / Windows)
NOMS_SYSTEME = {".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems", ".DS_Store",
                "System Volume Information", "$RECYCLE.BIN"}


def est_systeme(nom):
    return nom in NOMS_SYSTEME or nom.startswith("._")


def chemin_libre(chemin):
    """Ajoute « (2) », « (3) »... si le fichier existe déjà."""
    if not os.path.exists(chemin):
        return chemin
    base, ext = os.path.splitext(chemin)
    i = 2
    while os.path.exists(f"{base} ({i}){ext}"):
        i += 1
    return f"{base} ({i}){ext}"


class EspaceInsuffisant(Exception):
    pass


def verifier_espace(dossier):
    if shutil.disk_usage(dossier).free < MARGE_LIBRE:
        raise EspaceInsuffisant("Le disque de destination est presque plein.")


def copier_plage(lecteur, debut, longueur, destination):
    """Copie une zone de la source vers un fichier."""
    with open(destination, "wb") as f:
        pos, fin = debut, debut + longueur
        while pos < fin:
            n = min(4 * MIO, fin - pos)
            f.write(lecteur.lire(pos, n))
            pos += n
    app.rendre_a_l_utilisateur(destination)


# --------------------------------------------------------------------------- #
# 1. Recherche rapide : système de fichiers FAT32
# --------------------------------------------------------------------------- #

@dataclass
class Element:
    chemin: str          # chemin dans la clé, ex. « Photos/Vacances/IMG_001.jpg »
    taille: int
    supprime: bool
    cluster: int
    date: float          # horodatage, 0 si inconnu
    chance: str          # « Bonne », « Incertaine », « Faible »


def _date_fat(d, t):
    try:
        return time.mktime((1980 + (d >> 9), (d >> 5) & 15, d & 31,
                            t >> 11, (t >> 5) & 63, (t & 31) * 2, 0, 0, -1))
    except (ValueError, OverflowError):
        return 0


def _somme_lfn(nom11):
    s = 0
    for c in nom11:
        s = (((s & 1) << 7) + (s >> 1) + c) & 0xFF
    return s


class Fat32:
    @staticmethod
    def secteur_valide(bs):
        if len(bs) < 512 or bs[510:512] != b"\x55\xaa":
            return False
        bps, spc, res, nfat = struct.unpack_from("<HBHB", bs, 11)
        fat16, = struct.unpack_from("<H", bs, 22)
        total, taille_fat, _, _, racine = struct.unpack_from("<IIHHI", bs, 32)
        return (bps in (512, 1024, 2048, 4096) and spc in (1, 2, 4, 8, 16, 32, 64, 128)
                and res >= 1 and nfat in (1, 2) and fat16 == 0 and taille_fat > 0
                and total > 0 and racine >= 2)

    def __init__(self, lecteur, debut, bs):
        self.l, self.debut = lecteur, debut
        self.bps, self.spc, self.reserves, self.nb_fat = struct.unpack_from("<HBHB", bs, 11)
        self.total, self.taille_fat, _, _, self.racine = struct.unpack_from("<IIHHI", bs, 32)
        self.etiquette = bs[71:82].decode("ascii", "replace").strip()
        self.taille_cluster = self.bps * self.spc
        self.pos_fat = debut + self.reserves * self.bps
        self.pos_donnees = self.pos_fat + self.nb_fat * self.taille_fat * self.bps
        self.nb_clusters = (self.total - self.reserves - self.nb_fat * self.taille_fat) // self.spc
        self.fat = None

    def signature_fat_ok(self, numero=0):
        d = self.l.lire(self.pos_fat + numero * self.taille_fat * self.bps, 8)
        return len(d) == 8 and d[0] >= 0xF0 and d[1:4] == b"\xff\xff\x0f"

    def charger_fat(self):
        numero = 0 if self.signature_fat_ok(0) or self.nb_fat < 2 else 1
        n = min(self.taille_fat * self.bps, (self.nb_clusters + 2) * 4)
        data = self.l.lire(self.pos_fat + numero * self.taille_fat * self.bps, n)
        self.fat = array.array("I")
        self.fat.frombytes(data[:len(data) - len(data) % 4])
        if sys.byteorder == "big":
            self.fat.byteswap()

    def cluster_valide(self, c):
        return 2 <= c < self.nb_clusters + 2

    def suivant(self, c):
        return self.fat[c] & 0x0FFFFFFF if c < len(self.fat) else 0

    def chaine(self, c, maximum=None):
        clusters, vus = [], set()
        while self.cluster_valide(c) and c not in vus:
            vus.add(c)
            clusters.append(c)
            if maximum and len(clusters) >= maximum:
                break
            c = self.suivant(c)
        return clusters

    def pos_cluster(self, c):
        return self.pos_donnees + (c - 2) * self.taille_cluster

    def lire_clusters(self, clusters):
        """Lit des clusters en regroupant ceux qui se suivent."""
        morceaux, i = [], 0
        while i < len(clusters):
            j = i
            while j + 1 < len(clusters) and clusters[j + 1] == clusters[j] + 1 and j - i < 127:
                j += 1
            morceaux.append(self.l.lire(self.pos_cluster(clusters[i]), (j - i + 1) * self.taille_cluster))
            i = j + 1
        return b"".join(morceaux)

    def entrees(self, data):
        """Décode les entrées d'un dossier (y compris les entrées supprimées)."""
        resultats, lfn = [], []
        for i in range(0, len(data) - 31, 32):
            e = data[i:i + 32]
            if e[0] == 0:
                break
            supprime = e[0] == 0xE5
            attr = e[11]
            if attr == 0x0F:  # morceau de nom long
                if not supprime and e[0] & 0x40:
                    lfn = []
                lfn.append((supprime, e[13], e[1:11] + e[14:26] + e[28:32]))
                continue
            if attr & 0xC0 or (attr & 0x08 and not attr & 0x10):  # invalide ou nom de volume
                lfn = []
                continue
            nom11 = e[0:11]
            if nom11 in (b".          ", b"..         "):
                lfn = []
                continue
            base = bytearray(e[0:8])
            if base[0] == 0x05:
                base[0] = 0xE5
            base = base.decode("cp850", "replace").rstrip()
            ext = e[8:11].decode("cp850", "replace").rstrip()
            if e[12] & 0x08:
                base = base.lower()
            if e[12] & 0x10:
                ext = ext.lower()
            if supprime:
                base = "_" + base[1:]
            nom = base + ("." + ext if ext else "")

            if lfn and all(s == supprime for s, _, _ in lfn) and len({c for _, c, _ in lfn}) == 1 \
                    and (supprime or lfn[0][1] == _somme_lfn(nom11)):
                long = b"".join(p for _, _, p in reversed(lfn)).decode("utf-16-le", "replace")
                long = long.split("\x00", 1)[0].replace("￿", "")
                if long:
                    nom = long
            lfn = []

            hi, = struct.unpack_from("<H", e, 20)
            t, d, lo, taille = struct.unpack_from("<HHHI", e, 22)
            resultats.append({"nom": nom, "dossier": bool(attr & 0x10), "cluster": (hi << 16) | lo,
                              "taille": taille, "supprime": supprime, "date": _date_fat(d, t)})
        return resultats

    def chance(self, cluster, taille, supprime):
        if taille == 0:
            return "Bonne"
        if not self.cluster_valide(cluster):
            return "Faible"
        n = -(-taille // self.taille_cluster)
        if not supprime:
            return "Bonne" if len(self.chaine(cluster, n)) >= n else "Incertaine"
        if cluster + n > self.nb_clusters + 2:
            return "Faible"
        libres = sum(1 for c in range(cluster, cluster + n) if self.suivant(c) == 0)
        if libres == n:
            return "Bonne"
        return "Incertaine" if libres else "Faible"

    def analyser(self, annuler=None, signaler=None, masquer_systeme=True):
        if self.fat is None:
            self.charger_fat()
        elements, vus = [], set()
        pile = [("", self.racine, False)]
        while pile:
            if annuler is not None and annuler.is_set():
                break
            chemin, c, dossier_supprime = pile.pop()
            if c in vus or not self.cluster_valide(c):
                continue
            vus.add(c)
            if dossier_supprime:
                # Dossier supprimé : sa chaîne a été effacée, on lit son premier cluster
                # et on vérifie qu'il ressemble bien à un dossier (« . » et « .. »).
                data = self.lire_clusters([c])
                if data[0:11] != b".          ":
                    continue
            else:
                data = self.lire_clusters(self.chaine(c, 65536))
            for e in self.entrees(data):
                if masquer_systeme and (est_systeme(e["nom"]) or (e["supprime"] and e["nom"].startswith("_.")
                                                                 and not e["dossier"])):
                    continue
                supprime = e["supprime"] or dossier_supprime
                p = f"{chemin}/{nom_sur(e['nom'])}" if chemin else nom_sur(e["nom"])
                if e["dossier"]:
                    pile.append((p, e["cluster"], supprime))
                else:
                    elements.append(Element(p, e["taille"], supprime, e["cluster"], e["date"],
                                            self.chance(e["cluster"], e["taille"], supprime)))
            if signaler:
                signaler(len(elements))
        return elements

    def extraire(self, el, destination):
        """Copie un fichier de la clé vers `destination`. Renvoie le chemin écrit."""
        n = -(-el.taille // self.taille_cluster)
        if el.supprime:
            clusters = [c for c in range(el.cluster, el.cluster + n) if self.cluster_valide(c)]
        else:
            clusters = self.chaine(el.cluster, n)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        destination = chemin_libre(destination)
        reste = el.taille
        with open(destination, "wb") as f:
            for i in range(0, len(clusters), 128):
                if reste <= 0:
                    break
                bloc = self.lire_clusters(clusters[i:i + 128])[:reste]
                f.write(bloc)
                reste -= len(bloc)
        if el.date:
            try:
                os.utime(destination, (el.date, el.date))
            except OSError:
                pass
        app.rendre_a_l_utilisateur(destination)
        return destination


def trouver_fat32(lecteur):
    """
    Cherche un système FAT32 sur la source. Renvoie (Fat32 ou None, message).
    Essaie le secteur de démarrage normal, puis sa copie de secours, puis cherche
    dans les 64 premiers Mo si la table des partitions est elle aussi abîmée.
    """
    s0 = lecteur.lire(0, 512)
    debuts = [0]
    autres = set()
    if s0[510:512] == b"\x55\xaa":
        for i in range(4):
            e = s0[446 + 16 * i:462 + 16 * i]
            lba, = struct.unpack_from("<I", e, 8)
            if e[4] and lba and lba * 512 < lecteur.taille:
                debuts.append(lba * 512)

    def essayer(debut, bs):
        if Fat32.secteur_valide(bs):
            fs = Fat32(lecteur, debut, bs)
            if fs.signature_fat_ok(0) or (fs.nb_fat > 1 and fs.signature_fat_ok(1)):
                return fs
        return None

    for debut in debuts:
        bs = lecteur.lire(debut, 512)
        typ = bs[3:11]
        if typ == b"EXFAT   ":
            autres.add("exFAT")
        elif typ == b"NTFS    ":
            autres.add("NTFS")
        fs = essayer(debut, bs)
        if fs:
            return fs, "Système FAT32 trouvé."
        for secours in (6, 12):  # copie de secours du secteur de démarrage
            fs = essayer(debut, lecteur.lire(debut + secours * 512, 512))
            if fs:
                return fs, ("Le secteur de démarrage était abîmé : sa copie de secours a été utilisée.")

    # Recherche d'un secteur de démarrage dans les 64 premiers Mo
    limite = min(64 * MIO, lecteur.taille)
    for base in range(0, limite, MIO):
        bloc = lecteur.lire(base, MIO)
        for off in range(0, len(bloc), 512):
            bs = bloc[off:off + 512]
            if bs[510:512] != b"\x55\xaa" or not Fat32.secteur_valide(bs):
                continue
            for debut in (base + off, base + off - 6 * 512):
                if debut >= 0:
                    fs = essayer(debut, bs)
                    if fs:
                        return fs, "Système FAT32 retrouvé en dehors de sa position habituelle."
    if autres:
        return None, (f"Système {', '.join(sorted(autres))} détecté : la recherche rapide ne gère que FAT32. "
                      "Utilisez la recherche approfondie.")
    return None, ("Aucun système de fichiers FAT32 lisible. Utilisez la recherche approfondie, "
                  "qui retrouve les fichiers d'après leur format.")


# --------------------------------------------------------------------------- #
# 2. Recherche approfondie : reconnaissance des fichiers d'après leur format
# --------------------------------------------------------------------------- #

CATEGORIES = {
    "Photos": ("jpg", "png", "heic"),
    "Documents": ("pdf", "docx", "xlsx", "pptx", "odt", "ods", "odp", "epub"),
    "Vidéos": ("mp4", "mov", "m4v", "3gp", "avi"),
    "Audio": ("mp3", "wav", "m4a"),
    "Archives": ("zip",),
}
CATEGORIE_DE = {ext: cat for cat, exts in CATEGORIES.items() for ext in exts}

MOTIF = re.compile(rb"\xff\xd8\xff|\x89PNG\r\n\x1a\n|%PDF-|PK\x03\x04|ftyp|ID3[\x02-\x04]|RIFF")


def _chercher(l, debut, motif, limite):
    """Position absolue de `motif` entre debut et debut+limite, ou None."""
    pos, fin = debut, min(debut + limite, l.taille)
    while pos < fin:
        bloc = l.lire(pos, min(4 * MIO, fin - pos) + len(motif) - 1)
        k = bloc.find(motif)
        if k >= 0:
            return pos + k
        pos += 4 * MIO
    return None


def _jpeg(l, pos):
    maxi = 64 * MIO
    h = l.lire(pos + 2, 2)
    if len(h) < 2 or h[0] != 0xFF or not (0xE0 <= h[1] <= 0xEF or h[1] in (0xDB, 0xC0, 0xC2, 0xC4, 0xFE)):
        return None
    p, scan = pos + 2, False
    while p - pos < maxi:
        if not scan:
            h = l.lire(p, 4)
            if len(h) < 4 or h[0] != 0xFF:
                return None
            m = h[1]
            if m == 0xFF:
                p += 1
            elif m == 0xD9:
                return ("jpg", p + 2 - pos) if p + 2 - pos > 1024 else None
            elif 0xD0 <= m <= 0xD7 or m == 0x01:
                p += 2
            else:
                longueur = (h[2] << 8) | h[3]
                if longueur < 2:
                    return None
                p += 2 + longueur
                scan = m == 0xDA
        else:
            bloc = l.lire(p, 256 * 1024)
            if len(bloc) < 2:
                return None
            j = bloc.find(b"\xff")
            while 0 <= j < len(bloc) - 1 and (bloc[j + 1] == 0 or 0xD0 <= bloc[j + 1] <= 0xD7):
                j = bloc.find(b"\xff", j + 2)
            if j < 0 or j >= len(bloc) - 1:
                p += len(bloc) - 1
            else:
                p += j
                scan = False
    return None


def _png(l, pos):
    p = pos + 8
    while p - pos < 64 * MIO:
        h = l.lire(p, 8)
        if len(h) < 8:
            return None
        longueur, t = struct.unpack(">I4s", h)
        if not t.isalpha():
            return None
        p += 12 + longueur
        if t == b"IEND":
            return "png", p - pos
    return None


def _pdf(l, pos):
    e = _chercher(l, pos, b"%%EOF", 512 * MIO)
    if e is None:
        return None
    fin = e + 5
    while True:  # mises à jour successives : plusieurs « %%EOF »
        suivant = _chercher(l, fin, b"%%EOF", 2 * MIO)
        autre_pdf = _chercher(l, fin, b"%PDF-", 2 * MIO)
        if suivant is None or (autre_pdf is not None and autre_pdf < suivant):
            break
        fin = suivant + 5
    fin += len(l.lire(fin, 2)) - len(l.lire(fin, 2).lstrip(b"\r\n"))
    return "pdf", fin - pos


def _zip(l, pos):
    e = _chercher(l, pos, b"PK\x05\x06", 1024 * MIO)
    if e is None:
        return None
    eocd = l.lire(e, 22)
    if len(eocd) < 22:
        return None
    taille_cd, debut_cd, commentaire = struct.unpack_from("<IIH", eocd, 12)
    if debut_cd + taille_cd != e - pos:
        return None
    longueur = e + 22 + commentaire - pos
    ext = "zip"
    entete = l.lire(pos, 128)
    n, extra = struct.unpack_from("<HH", entete, 26)
    if entete[30:30 + n] == b"mimetype":
        mime = entete[30 + n + extra:30 + n + extra + 60]
        for cle, x in ((b"opendocument.text", "odt"), (b"opendocument.spreadsheet", "ods"),
                       (b"opendocument.presentation", "odp"), (b"epub", "epub")):
            if cle in mime:
                ext = x
    else:
        cd = l.lire(pos + debut_cd, min(taille_cd, 4 * MIO))
        for cle, x in ((b"word/", "docx"), (b"xl/", "xlsx"), (b"ppt/", "pptx")):
            if cle in cd:
                ext = x
                break
    return ext, longueur


BOITES_MP4 = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pnot",
              b"udta", b"pdin", b"moof", b"mfra", b"sidx", b"styp", b"prft", b"emsg", b"junk", b"idat"}


def _mp4(l, pos):
    h = l.lire(pos, 12)
    marque = h[8:12]
    p, vues = pos, set()
    while p - pos < 16 * 1024 * MIO:
        h = l.lire(p, 16)
        if len(h) < 8:
            break
        taille, t = struct.unpack(">I4s", h[:8])
        if t not in BOITES_MP4:
            break
        if taille == 1 and len(h) == 16:
            taille, = struct.unpack(">Q", h[8:16])
        if taille < 8:
            break
        vues.add(t)
        p += taille
    p = min(p, l.taille)
    if marque in (b"heic", b"heix", b"mif1", b"msf1", b"hevc"):
        return ("heic", p - pos) if b"meta" in vues else None
    if b"moov" not in vues:
        return None
    if marque == b"qt  ":
        ext = "mov"
    elif marque in (b"M4A ", b"M4B "):
        ext = "m4a"
    elif marque == b"M4V ":
        ext = "m4v"
    elif marque.startswith(b"3g"):
        ext = "3gp"
    else:
        ext = "mp4"
    return ext, p - pos


DEBITS = {3: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],
          2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}
FREQUENCES = {3: (44100, 48000, 32000), 2: (22050, 24000, 16000), 0: (11025, 12000, 8000)}


def _mp3(l, pos):
    h = l.lire(pos, 10)
    taille_id3 = ((h[6] & 0x7F) << 21) | ((h[7] & 0x7F) << 14) | ((h[8] & 0x7F) << 7) | (h[9] & 0x7F)
    p = pos + 10 + taille_id3 + (10 if h[5] & 0x10 else 0)
    trames = 0
    while p - pos < 64 * MIO:
        h = l.lire(p, 4)
        if len(h) < 4:
            break
        if h[:3] == b"TAG":
            p += 128
            break
        if h[0] != 0xFF or (h[1] & 0xE0) != 0xE0:
            break
        version, couche = (h[1] >> 3) & 3, (h[1] >> 1) & 3
        debit, freq, rembourrage = h[2] >> 4, (h[2] >> 2) & 3, (h[2] >> 1) & 1
        if version == 1 or couche != 1 or debit in (0, 15) or freq == 3:
            break
        kbps = DEBITS[3 if version == 3 else 2][debit]
        hz = FREQUENCES[version][freq]
        p += (144 if version == 3 else 72) * kbps * 1000 // hz + rembourrage
        trames += 1
    return ("mp3", p - pos) if trames >= 10 else None


def _riff(l, pos):
    h = l.lire(pos, 12)
    taille, = struct.unpack_from("<I", h, 4)
    forme = h[8:12]
    ext = {b"WAVE": "wav", b"AVI ": "avi"}.get(forme)
    if not ext or taille < 36:
        return None
    return ext, min(taille + 8, l.taille - pos)


def identifier(l, pos, signature):
    """Renvoie (extension, longueur) si un fichier complet commence à `pos`."""
    try:
        if signature.startswith(b"\xff\xd8"):
            return _jpeg(l, pos)
        if signature.startswith(b"\x89PNG"):
            return _png(l, pos)
        if signature == b"%PDF-":
            return _pdf(l, pos)
        if signature == b"PK\x03\x04":
            return _zip(l, pos)
        if signature == b"ftyp":
            return _mp4(l, pos)
        if signature.startswith(b"ID3"):
            return _mp3(l, pos)
        if signature == b"RIFF":
            return _riff(l, pos)
    except (struct.error, IndexError, KeyError):
        return None
    return None


def recherche_approfondie(lecteur, dossier, categories, signaler=None, annuler=None):
    """
    Parcourt toute la source et enregistre dans `dossier/<catégorie>/` les fichiers reconnus.
    Renvoie (compteur par catégorie, motif d'arrêt : None, « annulé » ou « espace »).
    """
    MORCEAU = 4 * MIO
    trouves = {cat: 0 for cat in categories}
    pos, saut, motif = 0, 0, None
    debut = time.time()
    while pos < lecteur.taille:
        if annuler is not None and annuler.is_set():
            motif = "annulé"
            break
        bloc = lecteur.lire(pos, MORCEAU + 16)
        for m in MOTIF.finditer(bloc):
            s = m.start() - (4 if m.group() == b"ftyp" else 0)
            if s < 0 or s >= MORCEAU:
                continue
            absolu = pos + s
            if absolu % 512 or absolu < saut:
                continue
            res = identifier(lecteur, absolu, m.group())
            if not res:
                continue
            ext, longueur = res
            cat = CATEGORIE_DE.get(ext)
            saut = absolu + longueur
            if cat not in trouves:
                continue
            try:
                verifier_espace(dossier)
            except EspaceInsuffisant:
                motif = "espace"
                break
            trouves[cat] += 1
            sous = os.path.join(dossier, cat)
            os.makedirs(sous, exist_ok=True)
            app.rendre_a_l_utilisateur(sous)
            copier_plage(lecteur, absolu, longueur,
                         chemin_libre(os.path.join(sous, f"{cat.lower()}_{sum(trouves.values()):05d}.{ext}")))
        if motif:
            break
        pos += MORCEAU
        if saut > pos:
            pos = saut - saut % 512
        if signaler:
            ecoule = max(time.time() - debut, 1e-6)
            signaler(min(pos, lecteur.taille), lecteur.taille, pos / ecoule, trouves)
    return trouves, motif
