"""Seed script to populate the database with stub opportunities."""
from datetime import date, timedelta
import random
from src.bidlens.database import SessionLocal, engine, Base
from src.bidlens.models import Opportunity

Base.metadata.create_all(bind=engine)

AGENCIES = [
    "Department of Defense",
    "Department of Health and Human Services",
    "General Services Administration",
    "Department of Homeland Security",
    "Department of Veterans Affairs",
    "Department of Energy",
    "National Aeronautics and Space Administration",
    "Environmental Protection Agency",
    "Department of Commerce",
    "Department of Transportation"
]

SET_ASIDES = [
    None, None, None,
    "Small Business",
    "8(a)",
    "HUBZone",
    "Service-Disabled Veteran-Owned",
    "Woman-Owned Small Business",
    "Economically Disadvantaged Women-Owned Small Business"
]

NAICS_CODES = [
    "541511", "541512", "541519", "541611", "541618",
    "541690", "541990", "561210", "611430", "518210"
]

SOLICITATION_TITLES = [
    "IT Support Services for Federal Network Infrastructure",
    "Cybersecurity Assessment and Penetration Testing",
    "Cloud Migration and Hosting Services",
    "Enterprise Software Development and Maintenance",
    "Data Analytics Platform Development",
    "Mobile Application Development Services",
    "Help Desk and End User Support Services",
    "Network Equipment and Installation",
    "Managed Security Operations Center Services",
    "Digital Transformation Consulting",
    "Artificial Intelligence and Machine Learning Solutions",
    "Legacy System Modernization",
    "Healthcare Information System Integration",
    "Financial Management System Upgrade",
    "Supply Chain Management Software Implementation"
]

RFI_TITLES = [
    "Market Research: Next Generation Cloud Services",
    "Sources Sought: Emerging Technology Solutions",
    "RFI: Quantum Computing Capabilities Assessment",
    "Industry Day: Zero Trust Architecture Implementation",
    "Sources Sought: AI/ML Training Data Services",
    "RFI: Sustainable IT Infrastructure Solutions",
    "Market Research: DevSecOps Platform Providers",
    "Sources Sought: Satellite Communications Services",
    "RFI: Blockchain Technology Applications",
    "Industry Engagement: Edge Computing Solutions"
]

def generate_opportunities():
    db = SessionLocal()
    
    db.query(Opportunity).delete()
    db.commit()
    
    opportunities = []
    today = date.today()
    
    for i, title in enumerate(SOLICITATION_TITLES):
        posted = today - timedelta(days=random.randint(5, 30))
        deadline = today + timedelta(days=random.randint(7, 60))
        
        opp = Opportunity(
            sam_notice_id=f"SOL-2026-{1000 + i:04d}",
            title=title,
            agency=random.choice(AGENCIES),
            opportunity_type=random.choice(["Solicitation", "Combined Synopsis/Solicitation"]),
            posted_date=posted,
            response_deadline=deadline,
            naics=random.choice(NAICS_CODES),
            set_aside=random.choice(SET_ASIDES),
            description=f"""This is a solicitation for {title.lower()}.

The contractor shall provide all necessary personnel, materials, equipment, and supervision to accomplish the requirements outlined in the Statement of Work.

Period of Performance: Base year plus four option years.

This procurement is being conducted in accordance with FAR Part 15, Contracting by Negotiation.

Interested parties should submit their proposals by the response deadline indicated above.""",
            sam_url=f"https://sam.gov/opp/{1000 + i:04d}"
        )
        opportunities.append(opp)
    
    for i, title in enumerate(RFI_TITLES):
        posted = today - timedelta(days=random.randint(1, 15))
        deadline = today + timedelta(days=random.randint(14, 45))
        
        opp = Opportunity(
            sam_notice_id=f"RFI-2026-{2000 + i:04d}",
            title=title,
            agency=random.choice(AGENCIES),
            opportunity_type=random.choice(["RFI", "Sources Sought", "Special Notice"]),
            posted_date=posted,
            response_deadline=deadline,
            naics=random.choice(NAICS_CODES),
            set_aside=None,
            description=f"""This is a Request for Information (RFI) regarding {title.lower().replace('rfi: ', '').replace('sources sought: ', '').replace('market research: ', '')}.

The purpose of this RFI is to gather information from industry to assist in defining requirements for a potential future procurement.

Responses to this RFI are voluntary and will not be used to evaluate potential contractors.

This is not a solicitation and does not obligate the Government in any way.

Please provide information about your company's capabilities and relevant experience.""",
            sam_url=f"https://sam.gov/opp/{2000 + i:04d}"
        )
        opportunities.append(opp)
    
    for i in range(5):
        posted = today - timedelta(days=random.randint(10, 45))
        deadline = today + timedelta(days=random.randint(30, 90))
        
        opp = Opportunity(
            sam_notice_id=f"AWD-2026-{3000 + i:04d}",
            title=f"Award Notice: Contract for IT Services - Task Order {i + 1}",
            agency=random.choice(AGENCIES),
            opportunity_type="Award Notice",
            posted_date=posted,
            response_deadline=deadline,
            naics=random.choice(NAICS_CODES),
            set_aside=random.choice(SET_ASIDES),
            description=f"This is an award notice for IT services contract task order {i + 1}.",
            sam_url=f"https://sam.gov/opp/{3000 + i:04d}"
        )
        opportunities.append(opp)
    
    db.add_all(opportunities)
    db.commit()
    
    print(f"Successfully seeded {len(opportunities)} opportunities!")
    db.close()

if __name__ == "__main__":
    generate_opportunities()
