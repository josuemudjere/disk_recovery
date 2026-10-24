#!/usr/bin/env python3
"""
Réparation de clé USB sans perte de données (macOS).

Principe : on SAUVEGARDE d'abord, on RÉPARE ensuite. Le programme ne formate jamais.

  1. Diagnostic        : vérification en lecture seule (diskutil verifyVolume)
  2. Image complète    : copie secteur par secteur de la clé vers un fichier .img,
                         en sautant les secteurs illisibles (remplacés par des zéros)
  3. Copie de fichiers : copie des fichiers lisibles vers un dossier
  4. Réparation        : diskutil repairVolume (fsck adapté : FAT32, exFAT, APFS, HFS+...)

Lancement :  python3 app.py   (le mot de passe du Mac est demandé pour lire la clé)
"""

import datetime
import os
import plistlib
import pwd
import shutil
import socket
import subprocess
import sys
import time

MIO = 1024 * 1024


# --------------------------------------------------------------------------- #
# Utilitaires
# --------------------------------------------------------------------------- #

def diskutil_plist(verb, *args):
    """Lance `diskutil <verb> -plist ...` et renvoie le dictionnaire résultant."""
    res = subprocess.run(["diskutil", verb, "-plist", *args], capture_output=True)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.decode(errors="replace").strip() or f"diskutil {verb} a échoué")
    return plistlib.loads(res.stdout)


def diskutil_live(*args):
    """Lance diskutil en affichant sa sortie en direct. Renvoie True si succès."""
    print(f"\n$ diskutil {' '.join(args)}\n")
    return subprocess.run(["diskutil", *args]).returncode == 0


def taille_lisible(n):
    for unite in ("o", "Ko", "Mo", "Go", "To"):
        if n < 1024 or unite == "To":
            return f"{n:.1f} {unite}" if unite != "o" else f"{n} o"
        n /= 1024


def demander(question, defaut=""):
    try:
        rep = input(question).strip()
    except EOFError:
        rep = ""
    return rep or defaut


def confirmer(question):
    return demander(f"{question} [o/N] ").lower() in ("o", "oui", "y", "yes")


def dossier_utilisateur():
    """Dossier personnel du vrai utilisateur, même sous sudo."""
    user = os.environ.get("SUDO_USER")
    if user:
        try:
            return pwd.getpwnam(user).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def rendre_a_l_utilisateur(chemin):
    """Sous sudo, les fichiers créés appartiennent à root : on les rend à l'utilisateur."""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and uid and gid:
        try:
            os.chown(chemin, int(uid), int(gid))
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Détection des clés USB
# --------------------------------------------------------------------------- #

def lister_disques_externes():
    """Renvoie la liste des disques externes physiques avec leurs volumes."""
    data = diskutil_plist("list", "external", "physical")
    disques = []
    for d in data.get("AllDisksAndPartitions", []):
        ident = d["DeviceIdentifier"]
        try:
            info = diskutil_plist("info", ident)
        except RuntimeError:
            continue
        if info.get("Internal", False):
            continue  # sécurité : on ne touche jamais un disque interne
        volumes = [p for p in d.get("Partitions", []) if p.get("Content") != "EFI"]
        if not volumes:  # clé formatée sans table de partitions
            volumes = [d]
        disques.append({
            "id": ident,
            "nom": info.get("MediaName", "Disque inconnu"),
            "taille": info.get("TotalSize") or info.get("Size") or d.get("Size", 0),
            "bloc": info.get("DeviceBlockSize", 512),
            "protocole": info.get("BusProtocol", "?"),
            "volumes": volumes,
        })
    return disques


def afficher_disque(disque):
    print(f"\n  /dev/{disque['id']} — {disque['nom']} — {taille_lisible(disque['taille'])} "
          f"({disque['protocole']})")
    for v in disque["volumes"]:
        nom = v.get("VolumeName") or "(sans nom)"
        fs = v.get("Content", "?")
        monte = v.get("MountPoint") or "non monté"
        print(f"     • {v['DeviceIdentifier']:<10} {nom:<20} {fs:<22} "
              f"{taille_lisible(v.get('Size', 0)):>10}   {monte}")


def choisir_disque():
    while True:
        try:
            disques = lister_disques_externes()
        except RuntimeError as e:
            print(f"Erreur : {e}")
            disques = []
        if not disques:
            print("\nAucune clé USB / disque externe détecté.")
            if demander("Branchez la clé puis appuyez sur Entrée (ou 'q' pour quitter) : ").lower() == "q":
                sys.exit(0)
            continue
        print("\nDisques externes détectés :")
        for i, d in enumerate(disques, 1):
            print(f"\n[{i}]", end="")
            afficher_disque(d)
        choix = demander("\nNuméro du disque à traiter (r = rafraîchir, q = quitter) : ").lower()
        if choix == "q":
            sys.exit(0)
        if choix.isdigit() and 1 <= int(choix) <= len(disques):
            return disques[int(choix) - 1]


def rafraichir(disque):
    """Relit l'état du disque (points de montage, etc.)."""
    for d in lister_disques_externes():
        if d["id"] == disque["id"]:
            return d
    return disque


# --------------------------------------------------------------------------- #
# 1. Diagnostic
# --------------------------------------------------------------------------- #

def diagnostic(disque):
    print("\n=== Diagnostic (lecture seule, aucune modification) ===")
    afficher_disque(disque)
    ok = True
    for v in disque["volumes"]:
        ok &= diskutil_live("verifyVolume", v["DeviceIdentifier"])
    print("\n→ Aucun problème détecté." if ok else
          "\n→ Des erreurs ont été trouvées. Faites une sauvegarde (option 2) avant de réparer.")


# --------------------------------------------------------------------------- #
# 2. Image complète secteur par secteur
# --------------------------------------------------------------------------- #

MARGE_LIBRE = 1024 * MIO  # on s'arrête avant de remplir complètement le disque de destination


def type_fs(chemin):
    """Type de système de fichiers (apfs, msdos, exfat...) de l'endroit où se trouve `chemin`."""
    chemin = os.path.realpath(chemin)
    meilleur, fs = "", "?"
    for ligne in subprocess.run(["mount"], capture_output=True, text=True).stdout.splitlines():
        # ex. : /dev/disk4s1 on /Volumes/CLE (msdos, local, nodev, ...)
        if " on " not in ligne or " (" not in ligne:
            continue
        point = ligne.split(" on ", 1)[1].rsplit(" (", 1)[0]
        if (chemin == point or chemin.startswith(point.rstrip("/") + "/")) and len(point) > len(meilleur):
            meilleur, fs = point, ligne.rsplit(" (", 1)[1].split(",")[0]
    return fs


def volume_du_disque(chemin, disque):
    """
    Si `chemin` se trouve sur un volume de `disque`, renvoie le nom de ce volume, sinon None.
    On compare les périphériques (st_dev) plutôt que les chemins, car les noms avec
    accents peuvent être écrits de deux façons différentes par macOS.
    """
    try:
        dev = os.stat(chemin).st_dev
    except OSError:
        return None
    for v in disque["volumes"]:
        point = v.get("MountPoint")
        if point:
            try:
                if os.stat(point).st_dev == dev:
                    return v.get("VolumeName") or v["DeviceIdentifier"]
            except OSError:
                pass
    return None


def verifier_destination(disque, destination):
    """Renvoie un message d'erreur, ou None si la destination convient."""
    dossier = os.path.dirname(os.path.abspath(destination))
    if not os.path.isdir(dossier):
        return f"Le dossier {dossier} n'existe pas."
    if volume_du_disque(dossier, disque):
        return (f"Ce dossier se trouve sur « {disque['nom']} », le disque que vous voulez copier.\n"
                "Choisissez un dossier sur un autre disque.")
    if type_fs(dossier) == "msdos" and disque["taille"] > 4 * 1024 ** 3 - 1:
        return ("Ce disque est en FAT32 : il n'accepte pas de fichier de plus de 4 Go.\n"
                "Choisissez un disque formaté en exFAT, APFS ou Mac OS étendu.")
    return None


AUTHOPEN = "/usr/libexec/authopen"


def ouvrir_avec_autorisation(chemin):
    """
    Ouvre `chemin` (ex. /dev/rdisk6) en lecture seule avec l'autorisation de macOS :
    le Mac affiche sa fenêtre de mot de passe, puis renvoie un descripteur de fichier.
    Le programme garde ainsi ses propres droits d'accès aux dossiers (Bureau, Documents...).
    """
    if os.geteuid() == 0:
        return os.open(chemin, os.O_RDONLY)
    parent, enfant = socket.socketpair()
    try:
        proc = subprocess.Popen([AUTHOPEN, "-stdoutpipe", chemin], stdout=enfant, stderr=subprocess.PIPE)
        enfant.close()
        _, fds, _, _ = socket.recv_fds(parent, 1024, 1)
        erreur = proc.communicate()[1].decode(errors="replace").strip()
    finally:
        parent.close()
    if not fds:
        raise PermissionError("Autorisation refusée ou annulée : la clé n'a pas pu être ouverte."
                              + (f"\n({erreur})" if erreur else ""))
    return fds[0]


def copier_peripherique(source, destination, taille, bloc=512, morceau=MIO, essais=3,
                        progression=None, annuler=None):
    """
    Copie `taille` octets de `source` vers `destination`.

    - Les zones illisibles sont relues secteur par secteur ; les secteurs vraiment
      morts sont remplacés par des zéros et listés dans <destination>.log.
    - Les zones vides (remplies de zéros) ne sont pas écrites : elles ne prennent
      pas de place sur le disque de destination (fichier « creux »).
    - La copie s'arrête proprement si le disque de destination est presque plein.

    `source` est un chemin ou un descripteur de fichier déjà ouvert (refermé à la fin).
    `progression(pos, taille, vitesse, reste_s, nb_defectueux)` est appelée régulièrement.
    On peut arrêter la copie avec `annuler` (threading.Event).

    Renvoie (octets_copiés, secteurs_défectueux, motif_arrêt) ;
    motif_arrêt vaut None (terminé), "annulé" ou "espace".
    """
    morceau = max(bloc, (morceau // bloc) * bloc)
    zeros = bytes(morceau)
    defectueux = []
    pos = 0
    debut = dernier_affichage = time.time()
    motif = None
    dossier = os.path.dirname(os.path.abspath(destination))
    compteur = 0

    def signaler():
        ecoule = max(time.time() - debut, 1e-6)
        vitesse = pos / ecoule
        reste = (taille - pos) / vitesse if vitesse else 0
        if progression:
            progression(pos, taille, vitesse, reste, len(defectueux))
        else:
            print(f"\r  {pos * 100 / taille:5.1f} %  {taille_lisible(pos)} / {taille_lisible(taille)}"
                  f"  {taille_lisible(vitesse)}/s  reste ~{int(reste // 60)} min {int(reste % 60):02d} s"
                  f"  secteurs illisibles : {len(defectueux)}   ", end="", flush=True)

    fd = source if isinstance(source, int) else os.open(source, os.O_RDONLY)
    try:
        with open(destination, "wb") as sortie:
            while pos < taille:
                if annuler is not None and annuler.is_set():
                    motif = "annulé"
                    break
                compteur += 1
                if compteur % 64 == 0 and shutil.disk_usage(dossier).free < MARGE_LIBRE:
                    motif = "espace"
                    break

                n = min(morceau, taille - pos)
                try:
                    os.lseek(fd, pos, os.SEEK_SET)
                    data = os.read(fd, n)
                    if not data:
                        raise OSError("fin de lecture inattendue")
                except OSError:
                    # Zone abîmée : on la relit secteur par secteur
                    buf = bytearray()
                    for s in range(pos, pos + n, bloc):
                        lu = None
                        for _ in range(essais):
                            try:
                                os.lseek(fd, s, os.SEEK_SET)
                                lu = os.read(fd, min(bloc, taille - s))
                                if lu:
                                    break
                            except OSError:
                                lu = None
                        if not lu:
                            defectueux.append(s // bloc)
                            lu = bytes(min(bloc, taille - s))
                        buf += lu
                    data = bytes(buf)

                if len(data) == morceau and data == zeros:
                    sortie.seek(len(data), os.SEEK_CUR)  # zone vide : on ne l'écrit pas
                else:
                    sortie.write(data)
                pos += len(data)

                if time.time() - dernier_affichage >= 0.5:
                    dernier_affichage = time.time()
                    signaler()
            sortie.truncate(pos)  # fixe la taille finale du fichier (zones vides comprises)
            sortie.flush()
            os.fsync(sortie.fileno())
    except KeyboardInterrupt:
        motif = "annulé"
    finally:
        os.close(fd)
    signaler()
    if not progression:
        print()

    if defectueux:
        with open(destination + ".log", "w") as log:
            log.write(f"Secteurs illisibles (taille de secteur : {bloc} octets), remplacés par des zéros :\n")
            log.writelines(f"{s}\n" for s in defectueux)
        rendre_a_l_utilisateur(destination + ".log")
    rendre_a_l_utilisateur(destination)
    return pos, defectueux, motif


def message_fin_image(destination, copie, defectueux, motif):
    """Texte expliquant le résultat d'une copie."""
    if motif == "annulé":
        return f"Copie annulée à {taille_lisible(copie)} : l'image est incomplète."
    if motif == "espace":
        return (f"Copie arrêtée à {taille_lisible(copie)} : le disque de destination est plein.\n"
                "L'image est incomplète. Branchez un disque externe assez grand "
                "(exFAT ou APFS) et recommencez en l'enregistrant dessus.")
    nb = defectueux if isinstance(defectueux, int) else len(defectueux)
    msg = f"Image créée : {destination}"
    if nb:
        msg += (f"\n{nb} secteur(s) illisible(s) remplacés par des zéros "
                f"(liste dans {os.path.basename(destination)}.log).")
    else:
        msg += "\nAucun secteur illisible : la copie est parfaite."
    return msg


def sauvegarde_image(disque):
    print("\n=== Sauvegarde : image complète de la clé ===")
    print("Copie de TOUTE la clé (même les fichiers supprimés ou une partition abîmée)")
    print("vers un fichier .img sur votre Mac. Rien n'est écrit sur la clé.")

    taille = disque["taille"]
    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    defaut = os.path.join(dossier_utilisateur(), "Desktop", f"sauvegarde_{disque['id']}_{horodatage}.img")
    destination = os.path.abspath(os.path.expanduser(demander(f"\nFichier de destination [{defaut}] : ", defaut)))
    erreur = verifier_destination(disque, destination)
    if erreur:
        print("⚠ " + erreur)
        return False
    libre = shutil.disk_usage(os.path.dirname(destination)).free
    print(f"\nTaille de la clé : {taille_lisible(taille)} — espace libre : {taille_lisible(libre)}")
    if libre < taille:
        print("⚠ L'espace libre est inférieur à la taille de la clé. Les zones vides de la clé ne prennent")
        print("  pas de place, donc l'image peut tenir si la clé n'est pas pleine. Sinon la copie")
        print("  s'arrêtera proprement avant de remplir votre disque.")
    if not confirmer("Démarrer la copie ? (la clé sera démontée pendant l'opération)"):
        return False

    # Démonter pour que macOS n'écrive rien sur la clé pendant la copie
    if not diskutil_live("unmountDisk", disque["id"]):
        if not confirmer("Le démontage a échoué (fichiers ouverts ?). Forcer le démontage ?"):
            return False
        if not diskutil_live("unmountDisk", "force", disque["id"]):
            return False

    source = f"/dev/r{disque['id']}"  # périphérique "raw" : plus rapide
    print("\nLe Mac va demander votre mot de passe pour lire la clé...")
    try:
        fd = ouvrir_avec_autorisation(source)
    except OSError as e:
        diskutil_live("mountDisk", disque["id"])
        print(f"⚠ {e}")
        return False
    print(f"\nCopie de {source} vers {destination}  (Ctrl+C pour interrompre)\n")
    copie, defectueux, motif = copier_peripherique(fd, destination, taille, disque["bloc"])
    print("\n" + message_fin_image(destination, copie, defectueux, motif))
    if motif:
        return False
    print("Vous pouvez l'ouvrir avec l'option 6 pour vérifier vos fichiers.")
    return True


# --------------------------------------------------------------------------- #
# 3. Copie des fichiers
# --------------------------------------------------------------------------- #

def sauvegarde_fichiers(disque):
    print("\n=== Sauvegarde : copie des fichiers lisibles ===")
    montes = [v for v in disque["volumes"] if v.get("MountPoint")]
    if not montes:
        print("Aucun volume monté. Essayez l'option 7 (remonter), sinon utilisez l'image complète (option 2).")
        return False

    horodatage = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    defaut = os.path.join(dossier_utilisateur(), "Desktop", f"fichiers_cle_{horodatage}")
    racine_dest = os.path.expanduser(demander(f"Dossier de destination [{defaut}] : ", defaut))

    total = erreurs = 0
    echecs = []
    for v in montes:
        src = v["MountPoint"]
        dest = os.path.join(racine_dest, v.get("VolumeName") or v["DeviceIdentifier"])
        print(f"\nCopie de {src} → {dest}")
        for dossier, sous_dossiers, fichiers in os.walk(src, onerror=lambda e: echecs.append(str(e))):
            sous_dossiers[:] = [d for d in sous_dossiers
                                if d not in (".Spotlight-V100", ".fseventsd", ".Trashes")]
            cible = os.path.join(dest, os.path.relpath(dossier, src))
            try:
                os.makedirs(cible, exist_ok=True)
            except OSError as e:
                echecs.append(f"{cible} : {e}")
                continue
            for f in fichiers:
                chemin = os.path.join(dossier, f)
                try:
                    shutil.copy2(chemin, os.path.join(cible, f))
                    total += 1
                    print(f"\r  {total} fichier(s) copiés, {erreurs} échec(s)   ", end="", flush=True)
                except (OSError, shutil.Error) as e:
                    erreurs += 1
                    echecs.append(f"{chemin} : {e}")
        print()

    if os.path.isdir(racine_dest):
        if echecs:
            with open(os.path.join(racine_dest, "fichiers_non_copies.txt"), "w") as log:
                log.write("\n".join(echecs) + "\n")
        for dossier, sous_dossiers, fichiers in os.walk(racine_dest):
            rendre_a_l_utilisateur(dossier)
            for f in fichiers:
                rendre_a_l_utilisateur(os.path.join(dossier, f))

    print(f"\n✔ {total} fichier(s) copiés dans {racine_dest}")
    if echecs:
        print(f"⚠ {len(echecs)} élément(s) illisible(s), voir fichiers_non_copies.txt. "
              "Pour ceux-là, préférez l'image complète (option 2).")
    return True


# --------------------------------------------------------------------------- #
# 4. et 5. Réparation
# --------------------------------------------------------------------------- #

def reparer_volumes(disque, sauvegarde_faite):
    print("\n=== Réparation du système de fichiers ===")
    print("Utilise l'outil de réparation de macOS (fsck). Aucun formatage.")
    if not sauvegarde_faite:
        print("\n⚠ Vous n'avez pas fait de sauvegarde pendant cette session.")
        print("  Une réparation corrige la structure du disque ; dans de rares cas, des fichiers")
        print("  très abîmés peuvent être déplacés ou supprimés. Sauvegardez d'abord (option 2).")
        if demander("\nTapez OUI pour réparer quand même : ") != "OUI":
            return
    elif not confirmer("Lancer la réparation ?"):
        return

    for v in disque["volumes"]:
        if diskutil_live("repairVolume", v["DeviceIdentifier"]):
            print(f"✔ {v['DeviceIdentifier']} réparé (ou déjà sain).")
        else:
            print(f"✘ Échec de la réparation de {v['DeviceIdentifier']}.")
            print("  Si la clé reste illisible, vos données sont toujours récupérables depuis l'image")
            print("  (option 2) avec un outil comme PhotoRec/TestDisk : brew install testdisk")
    diskutil_live("mountDisk", disque["id"])


def reparer_table_partitions(disque, sauvegarde_faite):
    print("\n=== Réparation de la table des partitions (avancé) ===")
    print("À utiliser seulement si la clé n'affiche aucune partition ou si macOS la dit 'non initialisée'.")
    if not sauvegarde_faite:
        print("⚠ Faites d'abord une image complète (option 2) : cette opération réécrit la table des partitions.")
        if demander("Tapez OUI pour continuer quand même : ") != "OUI":
            return
    diskutil_live("repairDisk", disque["id"])  # diskutil demande lui-même confirmation


# --------------------------------------------------------------------------- #
# 6. et 7. Divers
# --------------------------------------------------------------------------- #

def ouvrir_image():
    chemin = os.path.expanduser(demander("Chemin du fichier .img : ").strip("'\""))
    if not os.path.isfile(chemin):
        print("Fichier introuvable.")
        return
    print("Ouverture en lecture seule (l'image ne sera pas modifiée)...")
    subprocess.run(["hdiutil", "attach", "-readonly",
                    "-imagekey", "diskimage-class=CRawDiskImage", chemin])
    print("Les volumes de l'image apparaissent dans le Finder.")


def menu(disque):
    sauvegarde_faite = False
    while True:
        disque = rafraichir(disque)
        afficher_disque(disque)
        print("""
  1) Diagnostic (lecture seule)
  2) Sauvegarde : image complète de la clé   ← recommandé avant toute réparation
  3) Sauvegarde : copie des fichiers lisibles
  4) Réparer le système de fichiers
  5) Réparer la table des partitions (avancé)
  6) Ouvrir une image de sauvegarde (lecture seule)
  7) Remonter la clé
  8) Choisir un autre disque
  0) Quitter""")
        choix = demander("\nVotre choix : ")
        try:
            if choix == "1":
                diagnostic(disque)
            elif choix == "2":
                sauvegarde_faite |= sauvegarde_image(disque)
            elif choix == "3":
                sauvegarde_faite |= sauvegarde_fichiers(disque)
            elif choix == "4":
                reparer_volumes(disque, sauvegarde_faite)
            elif choix == "5":
                reparer_table_partitions(disque, sauvegarde_faite)
            elif choix == "6":
                ouvrir_image()
            elif choix == "7":
                diskutil_live("mountDisk", disque["id"])
            elif choix == "8":
                disque, sauvegarde_faite = choisir_disque(), False
            elif choix == "0":
                return
        except KeyboardInterrupt:
            print("\nOpération annulée.")
        except (OSError, RuntimeError) as e:
            print(f"\nErreur : {e}")


def main():
    if sys.platform != "darwin":
        sys.exit("Ce programme est prévu pour macOS.")
    print("=" * 64)
    print("  Réparation de clé USB — sans perte de données")
    print("  Règle d'or : sauvegarder d'abord, réparer ensuite.")
    print("=" * 64)
    try:
        menu(choisir_disque())
    except KeyboardInterrupt:
        print()
    print("Au revoir.")


if __name__ == "__main__":
    main()
