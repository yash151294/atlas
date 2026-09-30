"""Deterministic synthetic world for the demo. Every person, company and project is
fictional. Run: uv run python data/build_world.py -> data/world.json + salesforce export."""
import csv, json, random
from pathlib import Path

random.seed(20260917)
OUT = Path(__file__).parent

CONTRIBUTORS = [
 ("C01","Miriam Okonkwo-Hale","former FCDO diplomat, postings in Riyadh and Beirut","London","Gulf sovereign funds; energy; government relations; Levant politics"),
 ("C02","Tariq Al-Mansoori","former head of corporate banking at a UAE bank","Dubai","Gulf banking; compliance and licensing; family offices; real estate"),
 ("C03","Hannah Lindqvist","former Nordic financial regulator, licensing division","Stockholm","EU financial regulation; fintech licensing; payments; AML supervision"),
 ("C04","Rafael Duarte Nogueira","former oil-major executive turned adviser","Sao Paulo","Brazil energy; offshore; infrastructure concessions; Brasilia politics"),
 ("C05","Kenji Morimoto","former METI official, industrial policy bureau","Tokyo","Japan industrial policy; M&A approvals; trading houses; heavy industry"),
 ("C06","Priya Raghunathan","former strategy consultant, SE Asia practice","Singapore","SE Asia logistics; tech and e-commerce; PE deal flow; Malaysia and Indonesia"),
 ("C07","Douglas Whitcombe","former Metropolitan Police financial crime detective","London","investigations; AML; asset tracing; UK litigation support"),
 ("C08","Elena Vasquez-Moreno","former Mexican senate staffer and telecoms consultant","Mexico City","LatAm regulation; telecoms; Mexico politics; concessions"),
 ("C09","Samuel Adeyemi","former central bank supervisor","Lagos","West Africa banking; payments and fintech; central bank policy; Nigeria"),
 ("C10","Charlotte Beaumont","former forensic accounting partner","New York","US healthcare; pharma; litigation support; SEC matters"),
 ("C11","Viktor Hallstrom","former commodities hedge fund analyst","Zurich","commodities trading; Swiss private banking; metals; Geneva trading houses"),
 ("C12","Aisha Rahman","former business journalist","Karachi","South Asia media; textiles; Pakistan politics; family conglomerates"),
]

ORGS = {
 "O01":("Meridian Gulf Bank","bank","Abu Dhabi"), "O02":("Al-Sahra Sovereign Holdings","sovereign fund","Riyadh"),
 "O03":("Nordhavn Financial Supervisory Authority","regulator","Copenhagen"), "O04":("Baltic Pay AB","fintech","Stockholm"),
 "O05":("Aurora Offshore Energia SA","energy","Rio de Janeiro"), "O06":("Companhia Vale do Norte","mining","Belo Horizonte"),
 "O07":("Kurosawa Heavy Industries","industrial","Osaka"), "O08":("Nishikawa Trading Co","trading house","Tokyo"),
 "O09":("Straits Logistics Pte","logistics","Singapore"), "O10":("Tanjong Digital Ventures","venture capital","Singapore"),
 "O11":("Halcyon Capital Partners","private equity","London"), "O12":("Redwood Litigation Funders","litigation finance","London"),
 "O13":("Telcom Azteca SA","telecoms","Mexico City"), "O14":("Lagos Mercantile Bank","bank","Lagos"),
 "O15":("PayNaira Ltd","fintech","Lagos"), "O16":("Vantage Health Systems Inc","healthcare","Boston"),
 "O17":("Corvus Pharmaceuticals","pharma","New Jersey"), "O18":("Helvetia Commodities AG","commodities trader","Geneva"),
 "O19":("Alpenbank Privat AG","private bank","Zurich"), "O20":("Indus Textile Mills","textiles","Karachi"),
 "O21":("Karachi Herald Group","media","Karachi"), "O22":("Portland Assurance plc","insurer","London"),
 "O23":("Whitehall Rail Partners","infrastructure","London"), "O24":("Cobalt Ridge Minerals","mining","Perth"),
 "O25":("Gulf Fintech Regulatory Office","regulator","Abu Dhabi"), "O26":("Ministry of Mines and Energy (Brazil)","government","Brasilia"),
 "O27":("Japan Fair Trade Commission","regulator","Tokyo"), "O28":("Central Bank of Nigeria","regulator","Abuja"),
}

# id, name, role, org, region, topics, reached_by (contributor ids), former_orgs, name_variants
SOURCES = [
 ("S01","Farid Haddad","former Head of Compliance","O01","Gulf","AML controls; correspondent banking; licensing",["C02","C01"],["O25"],["F. Haddad","Fareed Haddad"]),
 ("S02","Layla Al-Qasimi","Senior Investment Director","O02","Gulf","sovereign co-investment; energy infrastructure; PE relationships",["C01"],[],["Layla al Qasimi"]),
 ("S03","Omar Bin Rashed","Head of Retail Banking","O01","Gulf","retail deposits; digital banking; branch strategy",["C02"],[],[]),
 ("S04","Nadia Kamel","Deputy Director, Licensing","O25","Gulf","fintech licensing; sandbox regime; regulatory timelines",["C02","C03"],[],["N. Kamel"]),
 ("S05","Hassan Al-Farsi","Family office principal","O02","Gulf","private capital; real estate; succession",["C02"],["O01"],[]),
 ("S06","Sofia Bergstrom","Head of Supervision, Payments","O03","Nordics","payment institution licences; AML supervision; EU passporting",["C03"],[],["Sofia Bergström"]),
 ("S07","Erik Vinter","Chief Executive Officer","O04","Nordics","fintech growth; fundraising; Baltic expansion",["C03"],[],[]),
 ("S08","Anders Holm","former Chief Financial Officer","O04","Nordics","unit economics; reporting practices; board dynamics",["C03"],["O03"],["A. Holm"]),
 ("S09","Joao Pedro Almeida","former Chief Operating Officer","O05","Brazil","offshore operations; safety record; contractor relationships",["C04"],["O26"],["João Pedro Almeida","JP Almeida"]),
 ("S10","Beatriz Cardoso","Director of Regulatory Affairs","O05","Brazil","licensing rounds; environmental approvals; ministry relations",["C04"],[],[]),
 ("S11","Marcelo Tavares","Secretary, Oil and Gas","O26","Brazil","concession policy; local content rules; ministerial priorities",["C04","C08"],[],["M. Tavares"]),
 ("S12","Luciana Ferreira","Head of Procurement","O06","Brazil","supplier integrity; tender process; logistics contracts",["C04"],[],[]),
 ("S13","Takeshi Kurosawa","Executive Vice President","O07","Japan","succession; take-private; family shareholding",["C05"],[],["T. Kurosawa"]),
 ("S14","Yumi Sato","General Manager, Corporate Planning","O07","Japan","strategy; mid-term plan; management dynamics",["C05"],[],[]),
 ("S15","Hiroshi Nakamura","Managing Director","O08","Japan","trading house relationships; joint ventures; financing",["C05"],[],[]),
 ("S16","Kenta Ishida","Senior Investigator","O27","Japan","merger review; market definition; remedies",["C05"],[],["K. Ishida"]),
 ("S17","Lim Wei Sheng","Chief Commercial Officer","O09","SE Asia","customer contracts; pricing; key accounts",["C06"],[],["Wei Sheng Lim","W.S. Lim"]),
 ("S18","Nurul Hidayah","former Head of Operations","O09","SE Asia","warehouse network; labour; Malaysia operations",["C06"],[],[]),
 ("S19","David Tan","Partner","O10","SE Asia","venture funding; cap table; founder reputation",["C06"],[],["D. Tan"]),
 ("S20","Arjun Mehta","Head of Key Accounts","O09","SE Asia","customer churn; contract renewals; competitor pricing",["C06","C12"],[],[]),
 ("S21","Richard Ellery","Investment Director","O11","UK","deal process; management assessment; portfolio governance",["C07","C01"],[],["R. Ellery"]),
 ("S22","Fiona McAllister","Head of Claims","O22","UK","insurance claims; fraud patterns; litigation exposure",["C07"],[],[]),
 ("S23","Gareth Pryce","former Finance Director","O23","UK","project finance; contractor disputes; cost overruns",["C07"],[],["G. Pryce"]),
 ("S24","Caroline Whitaker","Case Manager","O12","UK","funded claims; enforcement; asset recovery",["C07"],[],[]),
 ("S25","Alejandro Ruiz","Vice President, Regulatory","O13","LatAm","spectrum policy; concession renewals; regulator relations",["C08"],[],["A. Ruiz"]),
 ("S26","Mariana Solis","former Chief of Staff, telecoms regulator","O13","LatAm","regulatory decisions; political influence; timelines",["C08"],[],[]),
 ("S27","Carlos Ibarra","Head of Government Affairs","O13","LatAm","lobbying; congressional committees; policy trends",["C08"],[],[]),
 ("S28","Chinedu Okafor","Deputy Director, Payments Supervision","O28","West Africa","payment licences; FX policy; enforcement",["C09"],[],["C. Okafor"]),
 ("S29","Ngozi Eze","Chief Executive Officer","O15","West Africa","fintech growth; agent network; fundraising",["C09"],[],[]),
 ("S30","Emeka Nwosu","Head of Risk","O14","West Africa","credit risk; related-party lending; board oversight",["C09"],[],["E. Nwosu"]),
 ("S31","Folake Adebayo","former Head of Compliance","O15","West Africa","KYC gaps; regulatory correspondence; culture",["C09","C07"],["O14"],[]),
 ("S32","Dr. Rebecca Stone","Chief Medical Officer","O16","US","clinical governance; hospital acquisitions; physician relations",["C10"],[],["Rebecca Stone"]),
 ("S33","Michael Feldman","former General Counsel","O17","US","litigation history; FDA correspondence; settlements",["C10"],[],["M. Feldman"]),
 ("S34","Jennifer Park","Vice President, Market Access","O17","US","pricing; payer relations; pipeline",["C10"],[],[]),
 ("S35","Thomas Brennan","Head of Corporate Development","O16","US","M&A pipeline; valuation; integration",["C10","C11"],[],[]),
 ("S36","Lukas Meier","Head of Trading, Metals","O18","Switzerland","trading book; counterparties; sanctions screening",["C11"],[],["L. Meier"]),
 ("S37","Isabelle Fontaine","Senior Relationship Manager","O19","Switzerland","private banking; beneficial ownership; wealth structures",["C11"],[],[]),
 ("S38","Peter Zimmermann","former Chief Risk Officer","O18","Switzerland","risk limits; margin calls; internal disputes",["C11"],["O19"],["P. Zimmermann"]),
 ("S39","Claudia Roth","Compliance Officer","O19","Switzerland","onboarding; source of wealth; regulatory reporting",["C11"],[],[]),
 ("S40","Imran Sheikh","Managing Director","O20","South Asia","textile exports; family ownership; bank facilities",["C12"],[],["I. Sheikh"]),
 ("S41","Sana Malik","Business Editor","O21","South Asia","corporate reputation; media coverage; political links",["C12"],[],[]),
 ("S42","Bilal Chaudhry","former Chief Financial Officer","O20","South Asia","debt restructuring; related parties; audits",["C12"],[],["B. Chaudhry"]),
 ("S43","Grace Mwangi","Chief Operating Officer","O24","Australia","mine operations; community relations; cobalt supply",["C11","C06"],[],[]),
 ("S44","Simon Hartley","Head of Exploration","O24","Australia","reserves; permits; joint venture partners",["C06"],[],["S. Hartley"]),
 ("S45","Amira Nasser","Director, Sandbox Programme","O25","Gulf","sandbox admissions; fintech policy; regulator priorities",["C02"],[],[]),
]

PROJECTS = [
 ("PRJ-ORCHID","Commercial due diligence","Straits Logistics Pte (O09)","Commercial capabilities of management, customer contract integrity and competitive positioning ahead of a PE acquisition of Straits Logistics.","Halcyon Capital Partners",["C06","C12"],["S17","S18","S19","S20","S44"]),
 ("PRJ-BASALT","Reputational due diligence","Aurora Offshore Energia SA (O05)","Track record and reputation of Aurora Offshore Energia and its founder; safety incidents; regulatory relationships; procurement integrity.","a global investment fund",["C04","C08"],["S09","S10","S11","S12"]),
 ("PRJ-LANTERN","Regulatory intelligence","Gulf fintech licensing (O25, O01)","Forward-looking outlook for a payments licence in the Gulf: timelines, sandbox route, bank partnership expectations, regulator priorities.","Baltic Pay AB",["C02","C01","C03"],["S01","S03","S04","S05","S45","S06","S07","S08"]),
 ("PRJ-HERON","Management assessment","Kurosawa Heavy Industries (O07)","Succession, family shareholding and management dynamics at Kurosawa ahead of a take-private; merger review risk.","a global buyout fund",["C05"],["S13","S14","S15","S16"]),
 ("PRJ-SABLE","Dispute support and asset tracing","Helvetia Commodities AG (O18)","Asset trace and counterparty conduct in a dispute involving Helvetia Commodities; private banking relationships; risk breaches.","a law firm acting for a litigation funder",["C11","C07"],["S36","S37","S38","S39","S21","S22","S23","S24"]),
 ("PRJ-MARLIN","Pre-acquisition integrity screening","PayNaira Ltd (O15)","Integrity and regulatory standing of PayNaira and its leadership ahead of an acquisition; compliance culture; central bank relationship.","a pan-African bank",["C09","C07","C10"],["S28","S29","S30","S31","S32","S33","S34","S35"]),
]

# Transcript assignments: which contributor interviewed which source, for which project, when.
def assignments():
    A = []
    dates = iter(sorted(random.sample(range(0, 540), 60)))  # days since 2025-01-06
    def d():
        import datetime
        return (datetime.date(2025,1,6) + datetime.timedelta(days=next(dates))).isoformat()
    src = {s[0]: s for s in SOURCES}
    for pid, *_ , contribs, sources in PROJECTS:
        for sid in sources:
            s = src[sid]
            reach = [c for c in s[6] if c in contribs] or [s[6][0]]
            for c in reach:
                A.append(dict(project=pid, contributor=c, source=sid, date=d()))
    # two follow-up interviews with the same source months later ("over time")
    A.append(dict(project="PRJ-LANTERN", contributor="C02", source="S01", date="2026-06-11"))
    A.append(dict(project="PRJ-ORCHID", contributor="C06", source="S17", date="2026-07-02"))
    A.sort(key=lambda a: a["date"])
    for i, a in enumerate(A):
        a["id"] = f"T{i+1:03d}"
        a["fmt"] = "pdf" if i % 3 == 0 else "docx"
        a["naming"] = "filename" if i % 2 == 0 else "body"       # source named in filename or only in body
        a["style"] = "notes" if i % 7 == 6 else "dialogue"
    return A

def main():
    world = dict(
        contributors=[dict(zip(["id","name","background","base","access_keywords"], c)) for c in CONTRIBUTORS],
        organisations=[dict(id=k, name=v[0], sector=v[1], city=v[2]) for k, v in ORGS.items()],
        sources=[dict(zip(["id","name","role","org","region","topics","reached_by","former_orgs","name_variants"], s)) for s in SOURCES],
        projects=[dict(zip(["id","type","target","brief","client","contributors","sources"], p)) for p in PROJECTS],
        transcripts=assignments(),
    )
    (OUT / "world.json").write_text(json.dumps(world, indent=1))
    with (OUT / "raw/salesforce_contributors.csv").open("w", newline="") as f:
        w = csv.writer(f); w.writerow(["Contact_Id","Full_Name","Background__c","Base_City__c","Access_Keywords__c","Status__c","Onboarded__c"])
        for i, c in enumerate(CONTRIBUTORS):
            w.writerow([f"003{5000+i:06d}", c[1], c[2], c[3], c[4], "Active", f"2024-{(i%12)+1:02d}-15"])
    print(len(world["transcripts"]), "transcripts;", len(SOURCES), "sources;", len(CONTRIBUTORS), "contributors")

if __name__ == "__main__":
    main()
