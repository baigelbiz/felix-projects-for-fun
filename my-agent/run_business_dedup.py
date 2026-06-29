from drive_dedup import process, check_weird
business_files = process("BUSINESS (support@shefa.homes)", "drive_write_business_token.json")
check_weird("BUSINESS", business_files)
