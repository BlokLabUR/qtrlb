# =============================================================================
# All the function in this script are supposed to be purely mathematical without
# considering the parameter or dictionary structure of measurement, so that it 
# could also be called for other purpose.
# 
# Support both nested list and ndarray. Please make sure the first index is 0
# for I_data and 1 for Q_data, which make the typical input_data has shape
# (2, n_reps, x_points).
# 
# The input_data = np.array(input_data) not only guarantee the data format,
# but also protect the original object and keep it unchanged.
#
# All functions should support both Scan and Scan2D where the data for 1D has 
# shape (2, n_reps, x_points) and Scan2D has shape (2, n_reps, y_points, x_points).
# =============================================================================

import numpy as np
from sklearn.mixture import GaussianMixture
from sklearn.mixture._gaussian_mixture import _compute_precision_cholesky
PI = np.pi


def rotate_IQ(input_data: list | np.ndarray, angle: float):
    """
    Rotate all IQ data with angle in radian.
    """
    input_data = np.array(input_data)
    if angle < -2*PI or angle > 2*PI:
        print(f'Processing: Rotate angle {angle} may not in radian!')
        
    rot_matrix = [[np.cos(angle), -np.sin(angle)], 
                  [np.sin(angle), np.cos(angle)]]
    
    result = np.einsum('ij,j...->i...', rot_matrix, input_data)
    return result


def autorotate_IQ(input_data: list | np.ndarray, n_components: int):
    """
    Automatically rotate all IQ data based on most distance Gaussian blob.
    """
    input_data = np.array(input_data)
    means, covariances = gmm_fit(input_data, n_components=n_components)
    point_i, point_j = find_most_distant_points(means)
    angle = -1 * np.arctan2(point_i[1]-point_j[1], point_i[0]-point_j[0])
    result = rotate_IQ(input_data, angle)
    return result
    

def gmm_predict(input_data, means, covariances, covariance_type='spherical'):
    """
    Predict the state of input data based on given means and covariances of GMM.
    By default, means should have shape (n_components, 2) for 2D gaussian.
    Covariances should have shape (n_components,) for symmetrical distribution,
    where n_components is the number of Gaussian blob in IQ plane.
    
    Reference:
    https://scikit-learn.org/stable/modules/generated/sklearn.mixture.GaussianMixture.html
    """
    input_data = np.array(input_data)
    means = np.array(means)
    covariances = np.array(covariances)
    n_components = len(means)
    
    gmm = GaussianMixture(n_components, covariance_type=covariance_type)
    gmm.means_ = means
    gmm.covariances_ = covariances
    gmm.precisions_cholesky_ = _compute_precision_cholesky(covariances, covariance_type)
    gmm.weights_  = np.ones(n_components) / n_components

    result = gmm.predict(input_data.reshape(2,-1).T).reshape(input_data.shape[1:])
    # Magic reshape stealing from Ray.
    return result


def gmm_fit(input_data, n_components: int, covariance_type='spherical'):
    """
    Fit the input data with GMM. User must specify number of Gaussian blobs.
    The input_data should has shape (2, ...) because of two quadratures.
    Return the means and covariances. Means have shape (n_components, 2).
    Covariances have shape (n_components,) for symmetrical 2D distribution.
    """
    input_data = np.array(input_data)
    gmm = GaussianMixture(n_components, covariance_type=covariance_type)
    gmm.fit(input_data.reshape(2,-1).T)
    return gmm.means_, gmm.covariances_


def normalize_population(input_data, n_levels: int, axis: int = 0, mask: np.ndarray = None):
    """
    Count population (specific interger) for n_levels along a given axis.
    Return to normalized population (counts of appearing) with shape (n_levels, x_points).
    Allow a mask to pick entries in input_data to be normalized.
    Typically, the input_data and mask should have shape (n_reps, x_points).
    
    Example: 
        n_reps=4, x_points=3, two level system, no mask.
        data = [[0,1,0],[0,0,0],[0,1,0],[0,1,0]]
        result = [[1.0, 0.25, 1.0], [0.0, 0.75, 0.0]]
        So first x_point has 100% population in |0>, 0% in |1>.
        Second x_point has 25% population in |0>, 75% in |1>.
        Third x_point has 100% populaion in |0>, 0% in |1>.
    """
    # Zihao(02/17/2023): It's short, but still worth a function with clear explanation.
    masked_data = np.ma.MaskedArray(input_data, mask=mask)
    result = [np.mean(masked_data==level, axis=axis) for level in range(n_levels)]
    return np.array(result)


def correct_population(input_data, corr_matrix: list | np.ndarray):
    """
    Correct population based on a correction matrix (modification of confusion matrix).
    The element (row i, column j) in corr_matrix is P(predicted as state i | actually in state j).
    Thus, the corr_matrix times actual population gives predicted population.
    We have predicted by GMM and want to know hte actual result, so we use np.linalg.solve here.
    Unfortunately, if corr_matrix has shape (M, M), the shape of data can only be (M,) or (M, K).
    It means data with (M, K, N) etc will cause ValueError.
    So I choose to flat all other dimonsion and shape them back later.
    """
    input_data = np.array(input_data)
    flat_data = input_data.reshape(input_data.shape[0], -1)
    result = np.linalg.solve(corr_matrix, flat_data).reshape(input_data.shape)
    return result


def find_most_distant_points(input_data):
    """
    Find the most distant points of data based on Euclidean distance.
    The input_data should has shape (n_points, n_dimension).
    
    Note from Zihao(02/20/2023):
        It's O(N^2) now. I know there is better way to do that. 
        Please do it if you know how.
    """
    input_data = np.array(input_data)  
    max_distance = 0
    for i in input_data:
        for j in input_data:
            distance_ij = np.linalg.norm(i-j)
            if distance_ij > max_distance:
                max_distance = distance_ij
                point_i = i 
                point_j = j
    return point_i, point_j


def get_readout_fidelity(confusion_matrix: list | np.ndarray) -> float:
    """
    Calculate readout fidelity based on a given confusion matrix.
    We are using form of confusion matrix such that sum vertical elements give 1.
    
    Reference:
    https://arxiv.org/pdf/2208.05879.pdf
    Page 4, definition of F_a before Eq.(4)
    """
    confusion_matrix = np.array(confusion_matrix)
    fidelity = (1 + confusion_matrix[0, 0] - np.sum(confusion_matrix[0, 1:]) ) / 2
    return float(fidelity)