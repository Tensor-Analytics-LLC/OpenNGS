"""Scientific entrypoints whose dependencies live outside the core interpreter."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def scanpy_run(args):
    import numpy as np
    import scanpy as sc
    from scipy import sparse
    a = sc.read_h5ad(args.input)
    values = a.X.data if sparse.issparse(a.X) else a.X
    if a.n_obs < 4 or a.n_vars < 4 or not np.isfinite(values).all() or (values < 0).any():
        raise ValueError('Scanpy requires at least four cells and genes with finite non-negative counts')
    if args.neighbors >= a.n_obs:
        raise ValueError('Neighbour count must be smaller than the number of cells')
    a.layers['counts'] = a.X.copy()
    sc.pp.filter_cells(a, min_genes=1)
    if a.n_obs <= args.neighbors:
        raise ValueError('Too few nonempty cells for the requested neighbour count')
    sc.pp.normalize_total(a, target_sum=1e4)
    sc.pp.log1p(a)
    sc.pp.highly_variable_genes(a, n_top_genes=min(args.genes, a.n_vars))
    dimensions = min(50, a.n_obs - 1, int(a.var.highly_variable.sum()) - 1)
    if dimensions < 2:
        raise ValueError('At least three variable genes are required for PCA')
    sc.pp.pca(a, n_comps=dimensions, random_state=7341)
    sc.pp.neighbors(a, n_neighbors=args.neighbors, random_state=7341)
    sc.tl.umap(a, random_state=7341)
    sc.tl.leiden(a, resolution=args.resolution, random_state=7341, flavor='igraph', n_iterations=2, directed=False)
    a.uns['openngs_analysis'] = {'scanpy_version': sc.__version__, 'seed': 7341,
        'normalization_target': 10000, 'pca_dimensions': dimensions,
        'neighbors': args.neighbors, 'resolution': args.resolution,
        'scope': 'Exploratory clustering; batch correction and biological annotation are not inferred'}
    a.write_h5ad(args.output)


def snpeff_run(args, extra):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', args.assembly):
        raise ValueError('Invalid explicit SnpEff database identifier')
    anchor = Path(args.database)
    if anchor.name != 'snpEffectPredictor.bin':
        raise ValueError('Bind snpEffectPredictor.bin and its exact compatible binary companions')
    root = Path('/work/snpeff-database'); genome = root / args.assembly
    genome.mkdir(parents=True, exist_ok=True)
    for p in anchor.parent.iterdir():
        if p.is_file() and re.fullmatch(r'[A-Za-z0-9_.-]+\.bin(?:\.gz)?', p.name):
            shutil.copyfile(p, genome / p.name)
    config = Path('/work/snpeff.config')
    config.write_text('data.dir = '+str(root)+'\n'+args.assembly+'.genome : '+args.assembly+'\n')
    with open(args.output, 'wb') as target:
        subprocess.run(['/opt/annotation/bin/snpEff', '-c', str(config), '-dataDir', str(root),
            '-noStats', '-noLog', '-noDownload', args.assembly, args.variants, *extra], stdout=target, check=True,
            env={**os.environ,'JAVA_HOME':'/opt/annotation','PATH':'/opt/annotation/bin:'+os.environ.get('PATH','')})


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='operation', required=True)
    p = sub.add_parser('scanpy')
    p.add_argument('--input', required=True); p.add_argument('--output', required=True)
    p.add_argument('--genes', type=int, required=True); p.add_argument('--neighbors', type=int, required=True)
    p.add_argument('--resolution', type=float, required=True)
    p = sub.add_parser('snpeff')
    for key in ['database', 'assembly', 'variants', 'output']: p.add_argument('--'+key, required=True)
    args, extra = parser.parse_known_args()
    if args.operation == 'scanpy':
        if extra: raise ValueError('Scanpy accepts only declared scientific parameters')
        scanpy_run(args)
    else: snpeff_run(args, extra)

if __name__ == '__main__': main()
