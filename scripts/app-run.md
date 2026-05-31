## document preparation agent analyser

clear

/extract-tariff Publisher-Tariff-Book-FY-2025-26.pdf --page-start 5 --page-end 27 --two-column --version-tag FY2025-26-v2.0

python src/document_preparation_agent_v2.py --pass 1 --page-start 5 --page-end 13 --two-column-layout

python src/document_preparation_agent_v2.py --pass 2 --country "South Africa" --version-tag FY2025-26-v3.0

python src/document_preparation_agent_v2.py --pass all --page-start 5 --version-tag FY2025-26-v3.0
