from typing import List, Dict, Tuple
from enum import Enum
import logging

logger = logging.getLogger(__name__)

class OptimizerAlgorithm(str, Enum):
    FIRST_FIT_DECREASING = "ffd"
    BEST_FIT_DECREASING = "bfd"
    NEXT_FIT = "nf"
    # Toekomstig: GENETIC = "genetic"
    # Toekomstig: COLUMN_GENERATION = "column_gen"

class CuttingOptimizer:
    """
    Optimizes cutting plans using various bin packing algorithms
    """
    
    def __init__(self, stock_length: float, saw_kerf: float = 0.0, algorithm: OptimizerAlgorithm = OptimizerAlgorithm.FIRST_FIT_DECREASING):
        """
        Args:
            stock_length: Standard length of stock material
            saw_kerf: Width of saw blade (waste per cut)
            algorithm: Algorithm to use for optimization
        """
        self.stock_length = stock_length
        self.saw_kerf = saw_kerf
        self.algorithm = algorithm
    
    def optimize(self, required_pieces: List[Dict[str, any]]) -> Dict:
        """
        Optimize cutting plan for given pieces
        
        Args:
            required_pieces: List of dicts with 'length' and 'quantity' keys
            
        Returns:
            Dict with optimization results
        """
        if self.algorithm == OptimizerAlgorithm.FIRST_FIT_DECREASING:
            return self._optimize_ffd(required_pieces)
        elif self.algorithm == OptimizerAlgorithm.BEST_FIT_DECREASING:
            return self._optimize_bfd(required_pieces)
        elif self.algorithm == OptimizerAlgorithm.NEXT_FIT:
            return self._optimize_nf(required_pieces)
        else:
            raise ValueError(f"Unknown algorithm: {self.algorithm}")
    
    def _optimize_ffd(self, required_pieces: List[Dict[str, any]]) -> Dict:
        """First Fit Decreasing algorithm"""
        # Expand pieces by quantity
        all_pieces = []
        for piece in required_pieces:
            for _ in range(piece['quantity']):
                all_pieces.append({
                    'length': piece['length'],
                    'original_index': required_pieces.index(piece)
                })
        
        # Sort pieces by length (descending) - FFD algorithm
        all_pieces.sort(key=lambda x: x['length'], reverse=True)
        
        # Initialize bins (stock pieces)
        bins = []
        
        # Place each piece in first bin that fits
        for piece in all_pieces:
            piece_length = piece['length']
            placed = False
            
            for bin_data in bins:
                remaining = bin_data['remaining']
                
                # Check if piece fits (including saw kerf if not first piece)
                required_space = piece_length
                if bin_data['cuts']:
                    required_space += self.saw_kerf
                
                if required_space <= remaining:
                    bin_data['cuts'].append({
                        'length': piece_length,
                        'original_index': piece['original_index']
                    })
                    bin_data['remaining'] -= required_space
                    bin_data['used'] += piece_length
                    placed = True
                    break
            
            # If no bin fits, create new bin
            if not placed:
                bins.append({
                    'stock_length': self.stock_length,
                    'cuts': [{
                        'length': piece_length,
                        'original_index': piece['original_index']
                    }],
                    'used': piece_length,
                    'remaining': self.stock_length - piece_length
                })
        
        # Calculate statistics
        total_stock_used = len(bins)
        total_length_used = sum(bin_data['used'] for bin_data in bins)
        total_waste = (total_stock_used * self.stock_length) - total_length_used
        waste_percentage = (total_waste / (total_stock_used * self.stock_length)) * 100 if total_stock_used > 0 else 0
        
        # Format results
        cutting_plan = []
        for idx, bin_data in enumerate(bins, 1):
            cutting_plan.append({
                'stock_number': idx,
                'stock_length': self.stock_length,
                'cuts': bin_data['cuts'],
                'total_used': bin_data['used'],
                'waste': bin_data['remaining'],
                'efficiency': (bin_data['used'] / self.stock_length) * 100
            })
        
        result = {
            'algorithm': self.algorithm.value,
            'total_stock_used': total_stock_used,
            'total_waste': round(total_waste, 2),
            'waste_percentage': round(waste_percentage, 2),
            'cutting_plan': cutting_plan,
            'summary': {
                'total_pieces': len(all_pieces),
                'total_length_needed': total_length_used,
                'average_efficiency': round(100 - waste_percentage, 2)
            }
        }
        
        logger.info(f"Optimization ({self.algorithm.value}): {total_stock_used} stock pieces, {waste_percentage:.2f}% waste")
        
        return result
    
    def _optimize_bfd(self, required_pieces: List[Dict[str, any]]) -> Dict:
        """Best Fit Decreasing algorithm - plaats in bin met kleinste restant"""
        all_pieces = []
        for piece in required_pieces:
            for _ in range(piece['quantity']):
                all_pieces.append({
                    'length': piece['length'],
                    'original_index': required_pieces.index(piece)
                })
        
        all_pieces.sort(key=lambda x: x['length'], reverse=True)
        bins = []
        
        for piece in all_pieces:
            piece_length = piece['length']
            required_space = piece_length + (self.saw_kerf if bins else 0)
            
            # Find bin with smallest remaining space that fits
            best_bin = None
            smallest_remaining = float('inf')
            
            for bin_data in bins:
                space_needed = piece_length
                if bin_data['cuts']:
                    space_needed += self.saw_kerf
                
                if space_needed <= bin_data['remaining'] and bin_data['remaining'] < smallest_remaining:
                    best_bin = bin_data
                    smallest_remaining = bin_data['remaining']
            
            if best_bin:
                space_needed = piece_length
                if best_bin['cuts']:
                    space_needed += self.saw_kerf
                best_bin['cuts'].append({
                    'length': piece_length,
                    'original_index': piece['original_index']
                })
                best_bin['remaining'] -= space_needed
                best_bin['used'] += piece_length
            else:
                bins.append({
                    'stock_length': self.stock_length,
                    'cuts': [{
                        'length': piece_length,
                        'original_index': piece['original_index']
                    }],
                    'used': piece_length,
                    'remaining': self.stock_length - piece_length
                })
        
        return self._format_result(bins)
    
    def _optimize_nf(self, required_pieces: List[Dict[str, any]]) -> Dict:
        """Next Fit algorithm - probeer alleen huidige bin"""
        all_pieces = []
        for piece in required_pieces:
            for _ in range(piece['quantity']):
                all_pieces.append({
                    'length': piece['length'],
                    'original_index': required_pieces.index(piece)
                })
        
        all_pieces.sort(key=lambda x: x['length'], reverse=True)
        bins = []
        current_bin = None
        
        for piece in all_pieces:
            piece_length = piece['length']
            
            if current_bin:
                space_needed = piece_length
                if current_bin['cuts']:
                    space_needed += self.saw_kerf
                
                if space_needed <= current_bin['remaining']:
                    current_bin['cuts'].append({
                        'length': piece_length,
                        'original_index': piece['original_index']
                    })
                    current_bin['remaining'] -= space_needed
                    current_bin['used'] += piece_length
                    continue
            
            # Create new bin
            current_bin = {
                'stock_length': self.stock_length,
                'cuts': [{
                    'length': piece_length,
                    'original_index': piece['original_index']
                }],
                'used': piece_length,
                'remaining': self.stock_length - piece_length
            }
            bins.append(current_bin)
        
        return self._format_result(bins)
    
    def _format_result(self, bins: List[Dict]) -> Dict:
        """Format optimization result with statistics"""
        # Calculate statistics
        total_stock_used = len(bins)
        total_length_used = sum(bin_data['used'] for bin_data in bins)
        total_waste = (total_stock_used * self.stock_length) - total_length_used
        waste_percentage = (total_waste / (total_stock_used * self.stock_length)) * 100 if total_stock_used > 0 else 0
        
        # Format results
        cutting_plan = []
        for idx, bin_data in enumerate(bins, 1):
            cutting_plan.append({
                'stock_number': idx,
                'stock_length': self.stock_length,
                'cuts': bin_data['cuts'],
                'total_used': bin_data['used'],
                'waste': bin_data['remaining'],
                'efficiency': (bin_data['used'] / self.stock_length) * 100
            })
        
        result = {
            'algorithm': self.algorithm.value,
            'total_stock_used': total_stock_used,
            'total_waste': round(total_waste, 2),
            'waste_percentage': round(waste_percentage, 2),
            'cutting_plan': cutting_plan,
            'summary': {
                'total_pieces': sum(len(b['cuts']) for b in bins),
                'total_length_needed': total_length_used,
                'average_efficiency': round(100 - waste_percentage, 2)
            }
        }
        
        logger.info(f"Optimization ({self.algorithm.value}): {total_stock_used} stock pieces, {waste_percentage:.2f}% waste")
        
        return result
