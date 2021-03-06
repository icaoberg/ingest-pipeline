#! /usr/bin/env python

import csv
import os
from pathlib import Path
from metadata_file import MetadataFile
from type_base import MetadataError
from submodules import ingest_validation_tools_submission, ingest_validation_tools_error_report

class MetadataTSVMetadataFile(MetadataFile):
    """
    A metadata file type for the specialized metadata.tsv files used to store submission info
    """
    category_name = 'METADATATSV';

    def collect_metadata(self):
#         print('validating {} as metadata.tsv'.format(self.path))
#         dirpath = Path(os.path.dirname(self.path))
#         submission = ingest_validation_tools_submission.Submission(directory_path=dirpath,
#                                                                    ignore_files=os.path.basename(self.path))
#         report = ingest_validation_tools_error_report.ErrorReport(submission.get_errors())
#         if report.errors:
#             # Scan reports an error result
#             with open('ingest_validation_tools_report.txt', 'w') as f:
#                 f.write(report.as_text())
#             raise MetadataError('{} failed ingest validation test'.format(self.path))
        print('parsing metadatatsv from {}'.format(self.path))
        md = []
        with open(self.path, 'rU', newline='') as f:
            dialect = csv.Sniffer().sniff(f.read(256))
            f.seek(0)
            reader = csv.DictReader(f, dialect=dialect)
            for row in reader:
                dct = {k : v for k, v in row.items()}
                dct['_from_metadatatsv'] = True
                md.append(dct)
        return md
