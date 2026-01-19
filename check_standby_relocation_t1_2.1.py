#!/usr/bin/env python3
import os
import json
import requests
import urllib3
from getpass import getpass
from datetime import datetime

# Disabilito warning per certificati self-signed (se hai CA corretta, togli queste due righe)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def get_config_from_env_or_prompt():
    """
    Recupera NSX_MANAGER, NSX_USERNAME, NSX_PASSWORD dalle variabili di ambiente
    (NSX_MANAGER, NSX_USERNAME, NSX_PASSWORD). Se mancano, chiede all'utente.
    """
    nsx_manager = os.environ.get("NSX_MANAGER")
    username = os.environ.get("NSX_USERNAME")
    password = os.environ.get("NSX_PASSWORD")

    if not nsx_manager:
        nsx_manager = input("Inserisci NSX Manager (FQDN o IP): ").strip()

    if not username:
        username = input("Inserisci username NSX: ").strip()

    if not password:
        password = getpass("Inserisci password NSX: ")

    return nsx_manager, username, password


def create_session(nsx_manager, username, password, verify=False):
    """
    Crea una sessione requests verso NSX Manager.
    Se nel tuo ambiente usi token / login a sessione, qui puoi
    facilmente sostituire l'auth basic con la tua login().
    """
    session = requests.Session()
    session.verify = verify  # metti True se hai certificato valido
    session.auth = (username, password)
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Allow-Overwrite": "true",
    })
    base_url = f"https://{nsx_manager}"
    return session, base_url


def list_tier1_gateways(session, base_url):
    """
    Recupera tutti i Tier-1 gateways tramite Policy API:
    GET /policy/api/v1/infra/tier-1s
    """
    url = f"{base_url}/policy/api/v1/infra/tier-1s"
    results = []
    cursor = None

    while True:
        params = {}
        if cursor:
            params["cursor"] = cursor

        resp = session.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        results.extend(data.get("results", []))
        cursor = data.get("cursor")
        if not cursor:
            break

    return results


def get_t1_full_config(session, base_url, t1_id):
    """
    Recupera la configurazione COMPLETA di un singolo T1 tramite GET.
    GET /policy/api/v1/infra/tier-1s/{t1_id}
    """
    url = f"{base_url}/policy/api/v1/infra/tier-1s/{t1_id}"
    resp = session.get(url)
    resp.raise_for_status()
    return resp.json()


def save_t1_backup(t1_config, backup_dir="backups"):
    """
    Salva la configurazione del T1 in un file JSON nel formato:
    backups/T1_<id>_<timestamp>.json
    """
    if not os.path.exists(backup_dir):
        os.makedirs(backup_dir)
        print(f"[INFO] Creata directory '{backup_dir}' per i backup")
    
    t1_id = t1_config.get("id", "unknown")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"T1_{t1_id}_{timestamp}.json"
    filepath = os.path.join(backup_dir, filename)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(t1_config, f, indent=2, ensure_ascii=False)
    
    print(f"[BACKUP] Configurazione salvata in: {filepath}")
    return filepath


def update_t1_relocation(session, base_url, t1_id, enable_standby_relocation=True, backup_dir="backups"):
    """
    Metodo SICURO: fa GET dell'intero T1, salva backup, modifica solo enable_standby_relocation,
    poi fa PUT dell'intero oggetto.
    
    PUT /policy/api/v1/infra/tier-1s/{t1_id}
    """
    # 1. GET configurazione completa
    print(f"[INFO] GET configurazione completa T1 id={t1_id}...")
    t1_config = get_t1_full_config(session, base_url, t1_id)
    
    # 2. Salva backup PRIMA di modificare
    backup_file = save_t1_backup(t1_config, backup_dir)
    
    # 3. Modifica solo il campo enable_standby_relocation
    t1_config["enable_standby_relocation"] = enable_standby_relocation
    
    # 4. PUT della configurazione completa modificata
    url = f"{base_url}/policy/api/v1/infra/tier-1s/{t1_id}"
    print(f"[INFO] PUT configurazione modificata per T1 id={t1_id}...")
    r = session.put(url, json=t1_config)
    r.raise_for_status()
    
    print(f"[OK] Standby Relocation aggiornato su T1 id={t1_id} -> {enable_standby_relocation}")
    return r, backup_file


def classify_t1s(t1_list):
    """
    Classifica i T1 in:
      - tutti i T1
      - T1 con ha_mode=ACTIVE_STANDBY
      - tra questi, conformi (enable_standby_relocation=True)
      - tra questi, NON conformi (enable_standby_relocation=False o assente)
    """
    active_standby_all = []
    compliant = []
    non_compliant = []

    for t1 in t1_list:
        ha_mode = t1.get("ha_mode")
        if ha_mode == "ACTIVE_STANDBY":
            active_standby_all.append(t1)
            standby = t1.get("enable_standby_relocation", False)
            if standby:
                compliant.append(t1)
            else:
                non_compliant.append(t1)

    return active_standby_all, compliant, non_compliant


def print_report(t1_list, active_standby_all, compliant, non_compliant):
    """
    Stampa un report riepilogativo sulla situazione dei T1.
    """
    print("\n==================== REPORT TIER-1 NSX-T ====================")
    print(f"Totale T1 trovati:                     {len(t1_list)}")
    print(f"T1 in ha_mode=ACTIVE_STANDBY:          {len(active_standby_all)}")
    print(f"   ├─ già conformi (relocation=ON):    {len(compliant)}")
    print(f"   └─ NON conformi (relocation=OFF):   {len(non_compliant)}")
    print("=============================================================\n")

    if non_compliant:
        print("Dettaglio T1 NON conformi (verranno modificati se confermi):")
        print("-" * 90)
        for t1 in non_compliant:
            display_name = t1.get('display_name', '')
            t1_id = t1.get('id')
            print(
                f"NAME: {display_name:30}  "
                f"ID: {t1_id:28}  "
                f"ha_mode={t1.get('ha_mode')}  "
                f"enable_standby_relocation={t1.get('enable_standby_relocation', False)}"
            )
        print("-" * 90)
        print(f"Totale T1 da modificare: {len(non_compliant)}\n")
    else:
        print("Tutti i T1 in ACTIVE_STANDBY sono già conformi (relocation=ON).\n")


def select_t1s_to_modify(non_compliant):
    """
    Permette all'utente di scegliere quali T1 modificare:
    - tutti
    - selezionati manualmente tramite nome esatto (display_name o id)
    """
    print("\n==================== SELEZIONE T1 DA MODIFICARE ====================")
    print("Opzioni disponibili:")
    print("  'all'  o 'a'     : modifica TUTTI i T1 non conformi")
    print("  nomi separati    : es. 'gateway-prod,gateway-test' (usa display_name o id)")
    print("  'exit' o 'q'     : annulla operazione")
    print("\nNOTA: I nomi devono corrispondere ESATTAMENTE (case insensitive)")
    print("=" * 70)
    
    # Crea una mappa name/id -> T1 per ricerca facile
    t1_map = {}
    for t1 in non_compliant:
        display_name = t1.get('display_name', '').lower()
        t1_id = t1.get('id', '').lower()
        if display_name:
            t1_map[display_name] = t1
        t1_map[t1_id] = t1
    
    while True:
        selection = input("\nInserisci i nomi dei T1 (separati da virgola) o 'all': ").strip()
        
        if selection.lower() in ('exit', 'q', 'quit', 'cancel'):
            return []
        
        if selection.lower() in ('all', 'a', '*'):
            return non_compliant
        
        try:
            selected_t1s = []
            not_found = []
            names = [name.strip() for name in selection.split(',')]
            
            for name_input in names:
                name_lower = name_input.lower()
                
                # Cerca match esatto
                if name_lower in t1_map:
                    t1 = t1_map[name_lower]
                    if t1 not in selected_t1s:
                        selected_t1s.append(t1)
                else:
                    not_found.append(name_input)
            
            if not_found:
                print("\n⚠ Attenzione: I seguenti nomi NON sono stati trovati tra i T1 non conformi:")
                for name in not_found:
                    print(f"  - '{name}'")
            
            if not selected_t1s:
                print("\n[ERRORE] Nessun T1 valido trovato nella selezione")
                print("Riprova con nomi corretti (copia/incolla dal report) o digita 'all' per tutti\n")
                continue
            
            # Niente conferma: ritorna direttamente
            return selected_t1s
                
        except Exception as e:
            print(f"[ERRORE] Errore durante la selezione: {e}")
            print("Riprova con formato corretto (es. 'gateway-01,gateway-02' o 'all')\n")


def main():
    # 1. Config da env / prompt
    nsx_manager, username, password = get_config_from_env_or_prompt()

    # 2. Sessione verso NSX-T
    session, base_url = create_session(nsx_manager, username, password, verify=False)

    print(f"\nConnesso a NSX Manager: {nsx_manager}")
    print("Recupero elenco Tier-1 gateways da NSX-T Manager...")

    # 3. Elenca T1
    t1_list = list_tier1_gateways(session, base_url)

    # 4. Classifica
    active_standby_all, compliant, non_compliant = classify_t1s(t1_list)

    # 5. Report
    print_report(t1_list, active_standby_all, compliant, non_compliant)

    # Se non ce n'è nessuno da modificare, esco
    if not non_compliant:
        print("Nessuna modifica necessaria. Uscita.")
        return

    # 6. Selezione T1 da modificare
    selected_t1s = select_t1s_to_modify(non_compliant)
    
    if not selected_t1s:
        print("\nNessun T1 selezionato. Operazione annullata.")
        return

    # 7. Conferma finale
    print("\n" + "=" * 70)
    answer = input(f"CONFERMA: abilitare Standby Relocation sui {len(selected_t1s)} T1 selezionati? (yes/no): ")
    answer = answer.strip().lower()

    if answer not in ("y", "yes", "s", "si", "sì"):
        print("Operazione annullata.")
        return

    # 8. Applico GET + PUT sui T1 selezionati (metodo SICURO)
    print("\nProcedo con l'abilitazione (GET + PUT) di Standby Relocation sui T1 selezionati...")
    print("I backup delle configurazioni verranno salvati nella directory './backups'\n")
    
    success_count = 0
    error_count = 0
    backup_files = []
    
    for t1 in selected_t1s:
        try:
            _, backup_file = update_t1_relocation(session, base_url, t1["id"], enable_standby_relocation=True)
            backup_files.append(backup_file)
            success_count += 1
        except requests.HTTPError as e:
            error_count += 1
            print(f"[ERRORE] T1 id={t1.get('id')} – {e} – risposta: {e.response.text if e.response is not None else 'n/a'}")
        except Exception as e:
            error_count += 1
            print(f"[ERRORE] T1 id={t1.get('id')} – Errore generico: {e}")

    print("\n==================== RIEPILOGO OPERAZIONE ====================")
    print(f"T1 modificati con successo:  {success_count}")
    print(f"T1 con errori:               {error_count}")
    print(f"Backup salvati:              {len(backup_files)}")
    if backup_files:
        print(f"Directory backup:            ./backups/")
    print("=============================================================")
    print("\nOperazione completata.")


if __name__ == "__main__":
    main()